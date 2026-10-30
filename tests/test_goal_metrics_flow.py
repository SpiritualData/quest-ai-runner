"""The metric record in the deep goal loop: values kept by code, checked by the flow, handed to every consumer as data.

ESOI's rule for work is "what the relevant metric result was before and after your change". Here the goal loop is run for
real (only the verifier's verdict is scripted) with a worker that calls the real tool registry the way the deep worker's
shell command does, and the tests pin what the FLOW does with the ledger: the one request to record a result when a change
has none (once, never a loop, only where the runner's own records say something changed), the measured and the recorded
rows kept side by side, the payload on the result, the events and the text, and that nothing in all of it asks a model.

Offline: no model, no network, no real worker. The arm under the loop is the selected one (slice-and-writeback) unless a test
names another; with no specification records around, it reads nothing, so the gate tests below stand under it as they did.
"""
from __future__ import annotations

import re
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from esoi import scaffold

from esoi.metric_meter import AFTER, BEFORE, EVERY_MOMENT
from esoi.metric_results import NOT_RECORDED_LINE, MetricResult
from quest_ai_runner.config import build_orchestrator
from hubofhubs.ports.deep import DeepResult
from hubofhubs.ports.events import EVENT_MILESTONE, EVENT_RESULT, EVENT_STATUS, Mode
from quest_ai_runner.core.goal_iteration import DoctrineOnly, SliceAndWriteback, SliceOnly
from quest_ai_runner.core.goal_metrics import NUDGE_BRIEF
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig, PlanDecision
from quest_ai_runner.core.orchestrator_turn import _Emitter
from quest_ai_runner.core.resolve import default_name, names, resolve
from quest_ai_runner.core.tools import ToolContext, ToolResult, ToolSpec, build_tool_registry

from .conftest import StubProvider, StubRetrieval
from .test_fast_edit_ladder import _cfg, _orch
from .test_per_goal_context_iteration import ScriptedProvider

TASK = "task_1"
GREETING = "make the greeting function greet a person by name in the standard form"


class Timeline:
    """Everything the person was shown, in order."""

    def __init__(self) -> None:
        self.events: List[Any] = []
        self.lock = threading.Lock()

    def update(self, event, mode) -> None:
        with self.lock:
            self.events.append(event)

    def statuses(self) -> List[str]:
        return [e.text for e in self.events if e.type == EVENT_STATUS]


class Worker:
    """A deep worker that does its scripted steps through the REAL tool registry, as the shell command does.

    ``steps`` is one list per attempt: ``"change"`` (a tool that acts, which the tool-call ledger sees),
    ``("record", args)`` and ``("na", reason)`` (calls to record_metric_result, keyed by the run id the brief told it)."""

    def __init__(self, registry, steps, *, output="Changed the file.", session_id: Optional[str] = "sess-1",
                 working_dir: Optional[str] = None, write_receipt: bool = False, timeline: Optional[Timeline] = None,
                 landed: int = 0, task_id: Optional[str] = TASK):
        self.registry, self.steps, self.output, self.session_id = registry, list(steps), output, session_id
        self.write_receipt, self.timeline = write_receipt, timeline
        self.landed, self.task_id = landed, task_id   # file changes counted as landed; the id the worker's calls carry
        self.cfg = SimpleNamespace(working_dir=working_dir)
        self.calls: List[Dict[str, Any]] = []
        self.run_id: Optional[str] = None

    def run_goal(self, *, goal, brief, model=None, max_turns=None, resume_session_id=None, **kwargs):
        self.calls.append({"brief": brief, "resume": resume_session_id, "model": model})
        if self.timeline is not None:
            self.timeline.events.append(SimpleNamespace(type="worker", text=goal))
        found = re.search(r"--run (\w+)", brief)
        self.run_id = self.run_id or (found.group(1) if found else None)
        for step in (self.steps.pop(0) if self.steps else []):
            if step == "change":
                assert self.registry.invoke("write_things", {}, ToolContext(task_id=self.task_id,
                                                                            meta={"run_id": self.run_id})).ok
            elif step[0] == "record":
                out = self.registry.invoke("record_metric_result", step[1],
                                           ToolContext(task_id=self.task_id, meta={"run_id": self.run_id}))
                assert out.ok, out.text
            elif step[0] == "na":
                out = self.registry.invoke("record_metric_result", {"not_applicable": True, "reason": step[1]},
                                           ToolContext(task_id=self.task_id, meta={"run_id": self.run_id}))
                assert out.ok, out.text
        resumed = len(self.calls) > 1 and resume_session_id
        return DeepResult(met=True, output="Recorded it." if resumed else self.output, session_id=self.session_id,
                          has_write_receipt=self.write_receipt and not resumed,
                          changes_landed=0 if resumed else self.landed)


class FixedMeter:
    """A meter that answers from a list, one value per reading. ``cheap`` says whether a reading takes no time, ``serves``
    which moments (before the work, after it) it can answer."""
    name = "fixed"

    def __init__(self, *values, cheap: bool = False, serves=EVERY_MOMENT):
        self.values = list(values)
        self.reads = 0
        self.cheap = cheap
        self.serves = serves

    def measure(self, spec_slice, working_dir=None):
        self.reads += 1
        return self.values.pop(0) if self.values else None


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # a bare folder: no specification records around unless a test makes some
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = build_tool_registry({"QAR_METRIC_LEDGER_DIR": str(tmp_path / "metrics"),
                                    "QAR_TOOL_LEDGER_DIR": str(tmp_path / "tools")})
    registry.register(ToolSpec(name="write_things", description="writes", mutates=True,
                               handler=lambda args, ctx: ToolResult(ok=True, text="written")))
    return SimpleNamespace(registry=registry, tmp_path=tmp_path)


def orchestrate(world, worker, *, iteration=None, verdicts=None, max_iterations=None, provider=None):
    provider = provider or StubProvider([])
    orch = _orch(provider, [worker], verdicts or [{"met": True}] * 4)
    orch.tools = world.registry
    if iteration is not None:
        orch.goal_iteration = iteration
    if max_iterations is not None:
        orch.cfg.deep_goal_max_iterations = max_iterations
    return orch


def run_deep(orch, emit=None, task_id=TASK):
    plan = PlanDecision(action="deep", goal="g", deep_brief="b")
    return orch._run_deep(plan, "g", "sonnet", emit=emit, ctx_meta={"task_id": task_id}).deep_results[0]


def sources(done):
    return [m["source"] for m in done.metric_results]


# -- the gate: a change with no metric result is asked for once -------------------------------------

def test_a_changed_run_with_no_result_is_asked_once_in_its_own_session_and_the_answer_is_kept(world):
    worker = Worker(world.registry, [["change"], [("record", {"metric_id": "metric-x", "before": 0.5, "after": 0.7})]])
    provider = StubProvider([])
    orch = orchestrate(world, worker, provider=provider)
    done = run_deep(orch)
    assert len(worker.calls) == 2
    assert worker.calls[1]["resume"] == "sess-1" and worker.calls[1]["brief"] == NUDGE_BRIEF
    assert "--run" in worker.calls[0]["brief"] and "record_metric_result" in worker.calls[0]["brief"]
    assert done.met and not done.metric_missing and sources(done) == ["recorded"]
    # the work the first attempt did stays in the result, joined to the one sentence the second wrote
    assert done.output.startswith("Changed the file.") and "Recorded it." in done.output
    assert done.output.rstrip().endswith("(recorded)")
    assert provider.plan_calls == 0 and provider.answer_calls == 0   # not one model call anywhere in the flow


def test_the_one_request_is_not_repeated_after_a_worker_ignores_it(world):
    worker = Worker(world.registry, [["change"], []])
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 2   # asked once; accepted after; never a loop
    assert done.metric_missing and done.metric_results == []
    assert done.output.rstrip().endswith(NOT_RECORDED_LINE)


def test_the_request_to_record_yields_to_a_person_who_said_not_to_and_the_empty_ledger_is_the_outcome(world):
    """The nudge is one fixed text whatever the request says: the worker, who has the request, decides, and one that
    records nothing leaves the ledger empty, which ends as ``metric_missing`` without a third attempt."""
    assert "told you not to record a metric" in NUDGE_BRIEF and "say so in one sentence and stop" in NUDGE_BRIEF
    briefs = []
    for goal in ("g", "Change the note. I handle metrics myself, so do not call record_metric_result."):
        worker = Worker(world.registry, [["change"], []])
        orch = orchestrate(world, worker)
        done = orch._run_deep(PlanDecision(action="deep", goal=goal, deep_brief=goal), goal, "sonnet",
                              ctx_meta={"task_id": TASK}).deep_results[0]
        briefs.append(worker.calls[1]["brief"])
        assert len(worker.calls) == 2 and done.metric_missing and done.metric_results == []
    assert briefs == [NUDGE_BRIEF, NUDGE_BRIEF]


def test_a_declared_not_applicable_in_the_first_attempt_satisfies_the_rule(world):
    worker = Worker(world.registry, [["change", ("na", "Edited a draft; no metric measures it.")]])
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 1 and not done.metric_missing
    assert sources(done) == ["not_applicable"] and done.metric_results[0]["reason"].startswith("Edited a draft")
    assert done.output.rstrip().endswith("Metric: not applicable (Edited a draft; no metric measures it.)")


def test_a_recorded_result_in_the_first_attempt_means_no_request(world):
    worker = Worker(world.registry, [["change", ("record", {"metric_id": "m", "after": 0.9})]])
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 1 and sources(done) == ["recorded"]


def test_a_run_that_changed_nothing_is_never_asked(world):
    worker = Worker(world.registry, [[]])
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 1
    assert done.metric_results == [] and not done.metric_missing and done.output == "Changed the file."


def test_the_runners_write_receipts_count_as_a_change_too(world):
    worker = Worker(world.registry, [[], []], write_receipt=True)
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 2 and done.metric_missing


def test_a_runner_that_cannot_resume_a_session_is_not_asked_but_the_gap_is_marked(world):
    worker = Worker(world.registry, [["change"]], session_id=None)
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 1   # a re-run would repeat the work, so no request
    assert done.metric_missing and done.output.rstrip().endswith(NOT_RECORDED_LINE)


def test_with_no_attempt_left_the_gap_is_marked_not_asked(world):
    worker = Worker(world.registry, [["change"]])
    done = run_deep(orchestrate(world, worker, max_iterations=1))
    assert len(worker.calls) == 1 and done.metric_missing


def test_a_run_with_no_task_id_is_still_gated_from_its_run_id(world):
    """The tool-call ledger is keyed by the run id when there is no task, and the metric ledger always is."""
    worker = Worker(world.registry, [["change"], [("record", {"metric_id": "metric-x", "after": 0.7})]], task_id=None)
    plan = PlanDecision(action="deep", goal="g", deep_brief="b")
    done = orchestrate(world, worker)._run_deep(plan, "g", "sonnet", ctx_meta={}).deep_results[0]
    assert len(worker.calls) == 2 and worker.calls[1]["brief"] == NUDGE_BRIEF
    assert sources(done) == ["recorded"] and not done.metric_missing


def test_a_run_with_no_task_id_and_a_worker_that_ignores_the_request_is_marked(world):
    worker = Worker(world.registry, [["change"], []], task_id=None)
    plan = PlanDecision(action="deep", goal="g", deep_brief="b")
    done = orchestrate(world, worker)._run_deep(plan, "g", "sonnet", ctx_meta={}).deep_results[0]
    assert len(worker.calls) == 2 and done.metric_missing and done.metric_results == []


def test_changes_the_runner_counts_as_landed_gate_a_run_that_has_no_other_record(world):
    """A Claude Code worker reports no write receipt and its edits are no tool of ours: the landed count is the signal."""
    worker = Worker(world.registry, [[], [("na", "Edited a draft; no metric measures it.")]], landed=2, task_id=None)
    plan = PlanDecision(action="deep", goal="g", deep_brief="b")
    done = orchestrate(world, worker)._run_deep(plan, "g", "sonnet", ctx_meta={}).deep_results[0]
    assert len(worker.calls) == 2 and worker.calls[1]["brief"] == NUDGE_BRIEF
    assert sources(done) == ["not_applicable"] and not done.metric_missing
    assert done.changes_landed == 2   # the request's own session added none; the first attempt's count is kept


def test_a_run_whose_runner_counted_no_change_is_not_asked(world):
    worker = Worker(world.registry, [[]], landed=0, task_id=None)
    plan = PlanDecision(action="deep", goal="g", deep_brief="b")
    done = orchestrate(world, worker)._run_deep(plan, "g", "sonnet", ctx_meta={}).deep_results[0]
    assert len(worker.calls) == 1 and not done.metric_missing and done.metric_results == []


def test_the_gate_applies_only_when_the_worker_was_offered_the_tool(world):
    world.registry.unregister("record_metric_result")
    worker = Worker(world.registry, [["change"]])
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 1 and not done.metric_missing   # it could not have complied
    assert "record_metric_result" not in worker.calls[0]["brief"]


def test_a_task_id_alone_is_enough_for_a_call_made_without_the_run_id(world):
    class ForgetfulWorker(Worker):
        def run_goal(self, **kwargs):
            out = super().run_goal(**kwargs)
            if len(self.calls) == 1:   # records with only --task, as a worker that forgot --run would
                self.registry.invoke("record_metric_result", {"metric_id": "m", "after": 1}, ToolContext(task_id=TASK))
            return out

    worker = ForgetfulWorker(world.registry, [["change"]])
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 1 and sources(done) == ["recorded"]


def test_a_row_another_run_of_the_same_task_left_does_not_count(world):
    world.registry.metric_ledger.record(MetricResult(source="recorded", run_id=TASK, metric_id="old", after=1.0,
                                                     ts=1.0))   # an earlier run of this task, long ago
    worker = Worker(world.registry, [["change"], []])
    done = run_deep(orchestrate(world, worker))
    assert len(worker.calls) == 2 and done.metric_missing


# -- the facts come from the ledger, never from the words ----------------------------------------------

def test_a_number_the_worker_writes_in_its_text_changes_nothing_in_the_metrics(world):
    claim = "All done. Metric: Pass rate 0.10 -> 0.99 (measured). The metric moved a lot."
    worker = Worker(world.registry, [["change"], []], output=claim)
    done = run_deep(orchestrate(world, worker))
    assert done.metric_results == [] and done.metric_missing
    assert claim in done.output and done.output.rstrip().endswith(NOT_RECORDED_LINE)


def test_the_verifier_is_not_asked_before_the_request_is_answered(world):
    worker = Worker(world.registry, [["change"], [("record", {"metric_id": "m", "after": 1})]])
    seen = []
    orch = orchestrate(world, worker)
    orch._verify_goal = lambda goal, brief, output, **kw: (seen.append(output) or {"met": True}, None)
    run_deep(orch)
    assert len(seen) == 1 and "Recorded it." in seen[0]   # one verification, of the whole run, after the request


# -- the arms ------------------------------------------------------------------------------------------

@pytest.fixture
def repo(world):
    root = world.tmp_path / "repo"
    scaffold.init(root, "demo_pkg", say=lambda *_: None)
    return root


def request_for(orch, worker):
    plan = PlanDecision(action="deep", goal=GREETING, deep_brief=GREETING)
    return orch._run_deep(plan, GREETING, "sonnet", ctx_meta={"task_id": TASK}).deep_results[0]


def test_the_doctrine_arm_adds_nothing_to_the_brief_and_measures_nothing(world, repo):
    worker = Worker(world.registry, [["change", ("na", "A note.")]], working_dir=str(repo))
    done = request_for(orchestrate(world, worker, iteration=DoctrineOnly()), worker)
    assert "NEXT PRIORITY" not in worker.calls[0]["brief"] and sources(done) == ["not_applicable"]


def test_slice_only_puts_the_next_priority_in_the_brief_and_measures_nothing(world, repo, monkeypatch):
    monkeypatch.chdir(repo)   # the worker's shell starts in its working folder, which is where the tool finds the records
    worker = Worker(world.registry, [["change", ("record", {"metric_id": "greeting-pass-rate", "before": 0.5, "after": 1})]],
                    working_dir=str(repo))
    done = request_for(orchestrate(world, worker, iteration=SliceOnly()), worker)
    brief = worker.calls[0]["brief"]
    assert "NEXT PRIORITY" in brief and "greeting-pass-rate" in brief and "Share of sample names" in brief
    assert sources(done) == ["recorded"]
    assert done.metric_results[0]["name"] == "Greeting pass rate"   # the tool filled the name from the repository's records


def test_slice_and_writeback_measures_before_and_after_and_keeps_the_workers_row_beside_it(world, repo):
    timeline = Timeline()
    worker = Worker(world.registry, [["change", ("record", {"metric_id": "greeting-pass-rate", "before": 0.5, "after": 0.9})]],
                    working_dir=str(repo), timeline=timeline)
    meter = FixedMeter(0.5, 0.75)
    orch = orchestrate(world, worker, iteration=SliceAndWriteback(meters=[meter]))
    emit = _Emitter(timeline, Mode.LIVE, lambda text: None)
    plan = PlanDecision(action="deep", goal=GREETING, deep_brief=GREETING)
    done = orch._run_deep(plan, GREETING, "sonnet", emit=emit, ctx_meta={"task_id": TASK}).deep_results[0]
    assert meter.reads == 2 and len(worker.calls) == 1   # read before and after; the measured row means no request
    by_source = {m["source"]: m for m in done.metric_results}
    assert (by_source["measured"]["before"], by_source["measured"]["after"], by_source["measured"]["delta"]) == (0.5, 0.75, 0.25)
    assert (by_source["recorded"]["before"], by_source["recorded"]["after"]) == (0.5, 0.9)
    assert by_source["measured"]["disagrees"] and by_source["recorded"]["disagrees"]   # both kept, both flagged
    assert by_source["measured"]["solution_id"] == "greet-a-person" and by_source["measured"]["milestone"].startswith("Share")
    order = [e.text for e in timeline.events if e.type in (EVENT_STATUS, "worker")]
    before = order.index("Checking the starting value before any change.")
    after = order.index("Checking the result after the change.")
    assert before < order.index(GREETING) < after   # announced before the worker starts, and again once it is done


def test_a_recorded_row_serves_the_reading_before_and_the_arm_the_reading_after(world, repo):
    """Once the work is done the tree may differ from the one a recorded row describes, so the row is not asked again."""
    worker = Worker(world.registry, [["change"]], working_dir=str(repo))
    row, arm = FixedMeter(1.0, serves=(BEFORE,), cheap=True), FixedMeter(0.9977)
    done = request_for(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[row, arm])), worker)
    assert (row.reads, arm.reads) == (1, 1)
    assert [(m["before"], m["after"]) for m in done.metric_results] == [(1.0, 0.9977)]


def test_a_declared_not_applicable_is_not_shown_beside_a_reading_but_stays_in_the_ledger(world, repo):
    """A run showed "1 -> 1 (measured)" and "Nothing to measure" together: a reading beats a shrug."""
    worker = Worker(world.registry, [["change", ("na", "One sentence reworded; no metric covers prose.")]],
                    working_dir=str(repo))
    orch = orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.5, 0.6)]))
    done = request_for(orch, worker)
    assert sources(done) == ["measured"] and not done.metric_missing
    assert "not applicable" not in done.output and done.output.rstrip().endswith("(measured)")
    assert [r.source for r in orch.tools.metric_ledger.for_run(worker.run_id)].count("not_applicable") == 1
    alone = Worker(world.registry, [["change", ("na", "One sentence reworded; no metric covers prose.")]],
                   working_dir=str(repo), task_id="task_2")
    assert sources(request_for(orchestrate(world, alone, iteration=SliceAndWriteback(meters=[FixedMeter()])), alone)) == \
        ["not_applicable"]   # with no reading, the worker's declaration is all there is and it still shows


def test_with_no_meter_for_the_reading_after_the_workers_row_stands_beside_the_one_before(world, repo):
    worker = Worker(world.registry, [["change", ("record", {"metric_id": "greeting-pass-rate", "before": 1, "after": 1})]],
                    working_dir=str(repo))
    row = FixedMeter(1.0, serves=(BEFORE,), cheap=True)
    done = request_for(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[row])), worker)
    assert row.reads == 1   # the reading after the work was not taken from it
    by_source = {m["source"]: m for m in done.metric_results}
    assert (by_source["measured"]["before"], by_source["measured"]["after"]) == (1.0, None)   # no measured after is invented
    assert (by_source["recorded"]["before"], by_source["recorded"]["after"]) == (1, 1) and not done.metric_missing


def test_a_measured_row_alone_satisfies_the_rule_and_the_worker_is_not_asked(world, repo):
    worker = Worker(world.registry, [["change"]], working_dir=str(repo))
    done = request_for(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.5, 0.6)])), worker)
    assert len(worker.calls) == 1 and sources(done) == ["measured"] and not done.metric_missing
    assert done.output.rstrip().endswith("Metric: Greeting pass rate 0.5 -> 0.6 (measured)")


def test_a_meter_with_no_reading_writes_no_row_and_so_the_gate_still_stands(world, repo):
    worker = Worker(world.registry, [["change"], []], working_dir=str(repo))
    done = request_for(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter()])), worker)
    assert len(worker.calls) == 2 and done.metric_missing and done.metric_results == []


def test_an_unclear_request_gets_no_slice_and_no_block(world, repo):
    worker = Worker(world.registry, [["change", ("na", "Unrelated to any metric.")]], working_dir=str(repo))
    orch = orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.1, 0.2)]))
    plan = PlanDecision(action="deep", goal="write a poem about the sea", deep_brief="poem")
    done = orch._run_deep(plan, "write a poem about the sea", "sonnet", ctx_meta={"task_id": TASK}).deep_results[0]
    assert "NEXT PRIORITY" not in worker.calls[0]["brief"] and sources(done) == ["not_applicable"]


def test_a_run_that_never_started_is_not_measured_after(world, repo):
    class NeverStarts(Worker):
        def run_goal(self, **kwargs):
            self.calls.append(kwargs)
            return DeepResult(met=False, error="could not start", launch_failed=True)

    meter = FixedMeter(0.5, 0.6)
    worker = NeverStarts(world.registry, [], working_dir=str(repo))
    done = request_for(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[meter])), worker)
    assert meter.reads == 1   # the reading before; nothing to measure after a run that did not start
    assert not done.metric_missing and done.output == ""


# -- selection through resolve -------------------------------------------------------------------------

def test_the_three_arms_are_declared_and_the_runners_own_measuring_is_selected():
    assert default_name("iterate-by-spec") == "slice-and-writeback"
    assert names("iterate-by-spec") == ["generic-doctrine", "slice-and-writeback", "slice-only"]
    assert resolve("iterate-by-spec") is SliceAndWriteback
    assert resolve("iterate-by-spec", "slice-only") is SliceOnly
    assert resolve("iterate-by-spec", "generic-doctrine") is DoctrineOnly


def test_a_built_orchestrator_follows_the_selection_and_the_doctrine_is_the_off_switch():
    chosen = build_orchestrator(_cfg()).goal_iteration
    assert isinstance(chosen, SliceAndWriteback) and [m.name for m in chosen.meters] == ["recorded_row", "offline_arm"]
    assert isinstance(build_orchestrator(_cfg(selections={"iterate-by-spec": "generic-doctrine"})).goal_iteration,
                      DoctrineOnly)


def test_the_environment_turns_the_measuring_off_for_every_consumer(monkeypatch):
    monkeypatch.setenv("ESOI_SELECT_QUEST_AI_RUNNER_ITERATE_BY_SPEC", "generic-doctrine")
    assert isinstance(build_orchestrator(_cfg()).goal_iteration, DoctrineOnly)
    assert isinstance(Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]),
                                   registry=ModelRegistry(StubProvider([]))).goal_iteration, DoctrineOnly)


def test_a_consumer_that_builds_the_brain_itself_gets_the_selected_arm_too():
    """quest-backend constructs ``Orchestrator`` directly and never passes an arm."""
    provider = StubProvider([])
    brain = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider))
    assert isinstance(brain.goal_iteration, SliceAndWriteback)
    explicit = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                            goal_iteration=DoctrineOnly())
    assert isinstance(explicit.goal_iteration, DoctrineOnly)


# -- what every consumer is handed ---------------------------------------------------------------------

def deep_plan() -> Dict[str, Any]:
    return {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Do the work", "brief": "do it"}], "rationale": "deep"}


def test_the_result_and_both_events_carry_the_metrics_as_data(world):
    worker = Worker(world.registry, [["change", ("record", {"metric_id": "metric-x", "before": 0.25, "after": 0.5})]])
    provider = ScriptedProvider([deep_plan()], [{"met": True}])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=worker, tools=world.registry, config=OrchestratorConfig())
    timeline = Timeline()
    result = orch.run("please do the thing", sink=timeline, context_meta={"task_id": TASK})
    entry = {"solution_id": "", "metric_id": "metric-x", "name": "", "milestone": "", "before": 0.25, "after": 0.5,
             "delta": 0.25, "source": "recorded", "reason": "", "disagrees": False}
    assert result.metric_results == [entry] and result.metric_missing is False
    assert result.deep_results[0].metric_results == [entry]
    done = [e for e in timeline.events if e.type == EVENT_MILESTONE and (e.text or "").startswith("Completed")]
    final = [e for e in timeline.events if e.type == EVENT_RESULT and e.result_kind == "deep"]
    assert done[0].data["metrics"] == [entry] and final[0].data["metrics"] == [entry]
    assert "metric_missing" not in done[0].data and "metric_missing" not in final[0].data
    assert "Metric: metric-x 0.25 -> 0.5 (recorded)" in (final[0].text or "")


def test_a_missing_result_travels_as_a_flag_on_the_result_and_both_events(world):
    worker = Worker(world.registry, [["change"], []])
    provider = ScriptedProvider([deep_plan()], [{"met": True}])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=worker, tools=world.registry, config=OrchestratorConfig())
    timeline = Timeline()
    result = orch.run("please do the thing", sink=timeline, context_meta={"task_id": TASK})
    assert result.metric_results == [] and result.metric_missing is True
    done = [e for e in timeline.events if e.type == EVENT_MILESTONE and (e.text or "").startswith("Completed")]
    final = [e for e in timeline.events if e.type == EVENT_RESULT and e.result_kind == "deep"]
    for event in (done[0], final[0]):
        assert event.data["metrics"] == [] and event.data["metric_missing"] is True
    assert NOT_RECORDED_LINE in (final[0].text or "")


def test_a_turn_with_no_deep_goal_has_an_empty_record():
    provider = ScriptedProvider([{"action": "answer", "rationale": "easy", "model_tier": "sonnet"}], [])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        config=OrchestratorConfig())
    result = orch.run("what is two plus two")
    assert result.metric_results == [] and result.metric_missing is False


def test_no_status_the_flow_adds_uses_an_em_dash(world, repo):
    worker = Worker(world.registry, [["change"], []], working_dir=str(repo))
    orch = orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.5, 0.6)]))
    timeline = Timeline()
    emit = _Emitter(timeline, Mode.LIVE, lambda text: None)
    plan = PlanDecision(action="deep", goal=GREETING, deep_brief=GREETING)
    done = orch._run_deep(plan, GREETING, "sonnet", emit=emit, ctx_meta={"task_id": TASK}).deep_results[0]
    assert timeline.statuses() and not [t for t in timeline.statuses() if "—" in t]
    assert "—" not in done.output and "—" not in NUDGE_BRIEF
