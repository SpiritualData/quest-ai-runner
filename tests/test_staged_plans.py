"""Staged plans: the planner's ``deep_subtasks`` as a list of lists (core/orchestrator_staged_plan.py).

Pinned here, fully offline (scripted provider, recording runners):

* a flat list is normalized exactly as before, with the feature on or off, and a nested list is
  accepted only when ``OrchestratorConfig.staged_plans`` is on;
* the planner schema offers nesting only when the feature is on, and is otherwise the same object;
* the caps hold: top level, steps per sequence, tasks in all;
* a nested plan runs in process in order (a step of several tasks together), as the runner always could;
* a deep runner satisfying the ``StagedPlanRunner`` port receives each top-level sequence as its own
  plan of ordered stages and nothing runs in process; one that hands the first plan back (None) gets
  the in-process run; a flat plan or a plan with the feature off is never offered; the runner is the
  one a single task would run on (classifier, quest-data guard);
* the in-process run honours ``run_when`` (the two branches of a condition never both run) and a
  nested plan survives the Stop and no-executor paths;
* the planner is told about nesting when it is on, with the same prompt and schema bytes when it is off;
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
from quest_ai_runner.core.adapters_deep import StagedPlanRunner
from quest_ai_runner.core.orchestrator_staged_plan import (
    count_tasks, is_staged, normalize_staged_subtasks, sequence_stages, split_plan, step_allows, tasks_flat,
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

def test_a_sequence_becomes_ordered_stages_and_flat_tasks_stand_alone():
    subtasks = normalize_staged_subtasks(
        [task("X"), [task("A"), [task("B1"), task("B2", "prev_met")], task("C", "prev_not_met")]],
        max_items=4, max_stages=4, max_tasks=8)
    flat, plans = split_plan(subtasks)
    assert [t["goal"] for t in flat] == ["X"]
    assert len(plans) == 1
    assert [[t["goal"] for t in stage] for stage in plans[0]] == [["A"], ["B1", "B2"], ["C"]]
    assert is_staged(subtasks)
    assert not is_staged([task("A"), task("B")])
    assert is_staged([task("A", "prev_met")])


def test_independent_sequences_are_separate_plans_and_never_share_a_stage():
    subtasks = normalize_staged_subtasks(
        [[task("A1"), task("A2", "prev_met")], [task("B1"), task("B2", "prev_not_met"), task("B3")]],
        max_items=4, max_stages=4, max_tasks=8)
    flat, plans = split_plan(subtasks)
    assert flat == []
    assert [[[t["goal"] for t in stage] for stage in plan] for plan in plans] == [
        [["A1"], ["A2"]], [["B1"], ["B2"], ["B3"]]]


def test_nothing_comes_before_the_first_step_or_a_top_level_task_so_their_run_when_is_always():
    got = normalize_staged_subtasks([task("X", "prev_met"), [task("A", "prev_not_met"), task("B", "prev_met")]],
                                    max_items=4, max_stages=4, max_tasks=8)
    assert got[0]["run_when"] == "always" and got[1][0]["run_when"] == "always" and got[1][1]["run_when"] == "prev_met"


def test_tasks_flat_reaches_every_task_of_a_nested_plan():
    subtasks = normalize_staged_subtasks([task("X"), [task("A"), [task("B1"), task("B2")]]],
                                         max_items=4, max_stages=4, max_tasks=8)
    assert [t["goal"] for t in tasks_flat(subtasks)] == ["X", "A", "B1", "B2"]


def test_step_allows_reads_the_step_before():
    met, unmet = [DeepResult(met=True)], [DeepResult(met=False)]
    assert step_allows("prev_met", met) and not step_allows("prev_met", unmet)
    assert step_allows("prev_not_met", unmet) and not step_allows("prev_not_met", met)
    assert step_allows("always", unmet) and step_allows("prev_met", None) and step_allows(None, met)


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
    """A queue-style runner that takes a staged plan itself (it satisfies the StagedPlanRunner port)."""

    starts_background_work = True

    def __init__(self, answer: Optional[DeepResult]) -> None:
        self.answer = answer
        self.offers: List[Dict[str, Any]] = []
        self.goals: List[Dict[str, Any]] = []

    def run_staged_plan(self, *, goal: str, stages: List[List[Dict[str, Any]]],
                        context_preamble: Optional[str] = None) -> Optional[DeepResult]:
        self.offers.append({"goal": goal, "stages": stages})
        return self.answer

    def run_goal(self, *, goal: str, brief: str, **kw) -> DeepResult:
        self.goals.append({"goal": goal, "brief": brief})
        return DeepResult(met=True, deferred=True, output=f"queued {goal}")


class MisnamedRunner:
    """Offers a method that is NOT the port (a rename): the port check must fail, not silently pass."""

    starts_background_work = True

    def __init__(self) -> None:
        self.ran: List[str] = []

    def run_staged(self, *, goal, stages):  # noqa: D401
        raise AssertionError("must not be called")

    def run_goal(self, *, goal: str, brief: str, **kw) -> DeepResult:
        self.ran.append(goal)
        return DeepResult(met=True, output=goal)


def orchestrator(inline: Any, *, staged: bool = True, queue: Any = None, classifier: Any = None,
                 runners: Optional[Dict[str, Any]] = None, verdict: bool = True) -> Orchestrator:
    provider = ScriptedProvider([], [{"met": verdict}] * 40)
    registry = dict(runners or {})
    if queue is not None:
        registry[DEFERRED_RUNNER_KEY] = queue
    return Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        deep_runner=inline, deep_runners=(registry or None), deep_runner_classifier=classifier,
        config=OrchestratorConfig(staged_plans=staged))


class Sink:
    def update(self, event, mode) -> None:
        return None


def run_plan(orch: Orchestrator, subtasks: List[Any], **kw):
    plan = PlanDecision(action="deep", goal="Do the work", rationale="deep", deep_subtasks=subtasks, **kw.pop("plan_kw", {}))
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


class ScriptedMetRunner:
    """Runs in process; ``met_for`` says which goals meet their goal."""

    def __init__(self, met_for) -> None:
        self.met_for = met_for
        self.ran: List[str] = []
        self.lock = threading.Lock()

    def run_goal(self, *, goal: str, brief: str, model=None, max_turns=None, context_preamble=None) -> DeepResult:
        with self.lock:
            self.ran.append(goal)
        return DeepResult(met=goal in self.met_for, output=goal)


def ran_once(runner: Any) -> List[str]:
    """The goals a runner ran, in order, each once (a goal loop may retry an unmet goal)."""
    return list(dict.fromkeys(runner.ran))


def test_in_process_only_the_branch_whose_condition_holds_runs():
    # A, then B only if A met, then C only if the step before it (B, or A when B was skipped) did not meet.
    subtasks = normalize_staged_subtasks([[task("A"), task("B", "prev_met"), task("C", "prev_not_met")]],
                                         max_items=4, max_stages=4, max_tasks=8)
    met = ScriptedMetRunner({"A", "B"})
    run_plan(orchestrator(met), subtasks)
    assert ran_once(met) == ["A", "B"]  # A met -> B runs and met -> C (prev_not_met) is skipped
    unmet = ScriptedMetRunner(set())
    result = run_plan(orchestrator(unmet, verdict=False), subtasks)
    assert ran_once(unmet) == ["A", "C"]  # A not met -> B skipped; the last step that ran (A) did not meet -> C runs
    assert result.goals == ["A", "C"] and len(result.deep_results) == 2


def test_in_process_a_fallback_branch_runs_after_a_failed_step_and_is_skipped_after_a_good_one():
    subtasks = normalize_staged_subtasks([[task("A"), task("Fallback", "prev_not_met")]],
                                         max_items=4, max_stages=4, max_tasks=8)
    good, bad = ScriptedMetRunner({"A"}), ScriptedMetRunner(set())
    run_plan(orchestrator(good), subtasks)
    run_plan(orchestrator(bad, verdict=False), subtasks)
    assert ran_once(good) == ["A"] and ran_once(bad) == ["A", "Fallback"]


def test_a_nested_plan_does_not_crash_the_stop_path_and_names_its_goals():
    orch = orchestrator(RecordingRunner())
    subtasks = normalize_staged_subtasks([task("X"), [task("A"), [task("B1"), task("B2")]]],
                                         max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orch, subtasks, cancel_check=lambda: True)
    assert result.kind == "cancelled" and result.goals == ["X", "A", "B1", "B2"]


def test_a_nested_plan_does_not_crash_the_no_executor_path_and_names_its_goals():
    provider = ScriptedProvider([], [{"met": True}] * 4)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        config=OrchestratorConfig(staged_plans=True))
    subtasks = normalize_staged_subtasks([[task("A"), [task("B1"), task("B2")]]],
                                         max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orch, subtasks)
    assert result.kind == "deep" and result.goals == ["A", "B1", "B2"] and result.deep_results == []


def test_a_runner_that_takes_staged_plans_gets_each_sequence_as_its_own_plan_and_nothing_runs_in_process():
    inline = RecordingRunner()
    queue = OfferingRunner(DeepResult(met=True, deferred=True, output="Queued a plan"))
    assert isinstance(queue, StagedPlanRunner) and not isinstance(MisnamedRunner(), StagedPlanRunner)
    subtasks = normalize_staged_subtasks(
        [[task("A"), task("B", "prev_met"), task("C", "prev_not_met")], [task("D"), task("E")], task("F")],
        max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orchestrator(inline, queue=queue, classifier=lambda m, g, b: "delegate",
                                   runners={"delegate": queue}), subtasks, user_ordered=True)
    assert inline.calls == []
    assert [[[(t["goal"], t["run_when"]) for t in s] for s in o["stages"]] for o in queue.offers] == [
        [[("A", "always")], [("B", "prev_met")], [("C", "prev_not_met")]],
        [[("D", "always")], [("E", "always")]]]
    assert [g["goal"] for g in queue.goals] == ["F"]
    assert result.kind == "deep" and len(result.deep_results) == 3
    assert result.goals == ["A", "B", "C", "D", "E", "F"]


def test_the_pinned_runner_is_offered_the_plan():
    queue = OfferingRunner(DeepResult(met=True, deferred=True, output="Queued a plan"))
    subtasks = normalize_staged_subtasks([[task("A"), task("B", "prev_met")]], max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orchestrator(RecordingRunner()), subtasks, runner_override=queue, user_ordered=True)
    assert len(queue.offers) == 1 and result.deep_results[0].output == "Queued a plan"


def test_a_plan_goes_to_the_runner_a_single_task_would_run_on_not_always_the_queue():
    """The classifier chose an inline runner (quest data edits, say): the plan is not queued, it runs in process."""
    inline = RecordingRunner()
    queue = OfferingRunner(DeepResult(met=True, output="x"))
    subtasks = normalize_staged_subtasks([[task("A"), task("B")]], max_items=4, max_stages=4, max_tasks=8)
    orch = orchestrator(inline, queue=queue, classifier=lambda m, g, b: "code", runners={"code": inline})
    result = run_plan(orch, subtasks)
    assert queue.offers == [] and [c["goal"] for c in inline.calls] == ["A", "B"]
    assert result.kind == "deep"


def test_the_queue_key_is_never_chosen_by_the_classifier_for_a_plan():
    inline = RecordingRunner()
    queue = OfferingRunner(DeepResult(met=True, output="x"))
    subtasks = normalize_staged_subtasks([[task("A"), task("B")]], max_items=4, max_stages=4, max_tasks=8)
    run_plan(orchestrator(inline, queue=queue, classifier=lambda m, g, b: DEFERRED_RUNNER_KEY), subtasks)
    assert queue.offers == []


def test_a_quest_data_plan_gets_the_same_write_surface_instruction_a_single_task_would():
    queue = OfferingRunner(DeepResult(met=True, deferred=True, output="Queued a plan"))
    subtasks = normalize_staged_subtasks([[task("A"), task("B", "prev_met")]], max_items=4, max_stages=4, max_tasks=8)
    run_plan(orchestrator(RecordingRunner()), subtasks, runner_override=queue, user_ordered=True,
             plan_kw={"deep_target": "quest_data"})
    _, expected_brief = __import__("quest_ai_runner.core.orchestrator_decisions", fromlist=["x"]).apply_quest_data_ladder_guard(
        [queue], "do A", deliberate_choice=True)
    assert queue.offers[0]["stages"][0][0]["brief"] == expected_brief
    assert expected_brief != "do A"


def test_a_misnamed_runner_method_is_not_a_staged_runner_so_the_plan_runs_in_process():
    misnamed = MisnamedRunner()
    subtasks = normalize_staged_subtasks([[task("A"), task("B")]], max_items=4, max_stages=4, max_tasks=8)
    run_plan(orchestrator(misnamed), subtasks)
    assert ran_once(misnamed) == ["A", "B"]


def test_a_runner_that_hands_the_first_plan_back_gets_the_in_process_run():
    inline = RecordingRunner()
    queue = OfferingRunner(None)
    subtasks = normalize_staged_subtasks([[task("A"), task("B")]], max_items=4, max_stages=4, max_tasks=8)
    result = run_plan(orchestrator(inline, queue=queue, classifier=lambda m, g, b: "delegate",
                                   runners={"delegate": queue}), subtasks)
    assert len(queue.offers) == 1
    # handed back: the plan runs in process, step by step, on the runner each task resolves to
    assert [g["goal"] for g in queue.goals] == ["A", "B"] and inline.calls == []
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


# --- telling the planner ---------------------------------------------------------------------------------

def test_the_compact_schema_keeps_the_nesting_description_only_when_staged_plans_are_on():
    off = decide_tool_for(False, False, compact=True)["input_schema"]["properties"]["deep_subtasks"]
    on = decide_tool_for(False, False, compact=True, staged_plans=True)["input_schema"]["properties"]["deep_subtasks"]
    assert "description" not in off
    assert "LIST" in on["description"] and "run_when" in on["description"]
    assert decide_tool_for(False, False, compact=True) == decide_tool_for(False, False, compact=True, staged_plans=False)


def test_the_planner_prompt_stops_calling_every_entry_a_concurrent_run_only_when_staged_plans_are_on():
    from quest_ai_runner.core.planner_prompt import (
        DEEP_FAN_OUT, DEEP_FAN_OUT_CONCURRENT, DEEP_FAN_OUT_STAGED, PLANNER_PROFILES,
    )
    assert DEEP_FAN_OUT_CONCURRENT in DEEP_FAN_OUT
    for template in PLANNER_PROFILES.values():
        assert DEEP_FAN_OUT_CONCURRENT in template
    from quest_ai_runner.core.orchestrator_planning import PlanningMixin  # noqa: F401
    captured: Dict[str, str] = {}

    class Capture(ScriptedProvider):
        def plan(self, prompt, **kw):
            captured["prompt"] = prompt
            return {"action": "answer", "answer": "ok"}

    for staged in (False, True):
        provider = Capture([], [{"met": True}])
        orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                            deep_runner=RecordingRunner(), config=OrchestratorConfig(staged_plans=staged))
        orch._plan("do two things", "", "", [])
        assert (DEEP_FAN_OUT_STAGED in captured["prompt"]) is staged
        assert (DEEP_FAN_OUT_CONCURRENT in captured["prompt"]) is (not staged)


def test_turning_staged_plans_off_adds_no_prompt_or_schema_tokens():
    import json
    from quest_ai_runner.core.prompt_budget import estimate_tokens
    base = estimate_tokens(json.dumps(decide_tool_for(False, False), separators=(",", ":")))
    off = estimate_tokens(json.dumps(decide_tool_for(False, False, staged_plans=False), separators=(",", ":")))
    base_c = estimate_tokens(json.dumps(decide_tool_for(False, False, compact=True), separators=(",", ":")))
    off_c = estimate_tokens(json.dumps(decide_tool_for(False, False, compact=True, staged_plans=False),
                                       separators=(",", ":")))
    assert (off - base, off_c - base_c) == (0, 0)
