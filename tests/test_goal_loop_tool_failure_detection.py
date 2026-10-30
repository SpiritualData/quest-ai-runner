"""A broken tool is recognised from the runner's own ledger of tool calls, whatever the verifier says.

The verifier's ``tool_failure`` label is a model's judgement and can be wrong: a goals read that
keeps answering a server error was labelled ``more_work`` and the loop ran eight attempts, a
stronger model each time. These tests script the verifier to MISLABEL the gap and assert the goal
loop still stops after two attempts on one model, and that two attempts which end in the same
place with nothing new done also stop. The worker is simulated by a runner that calls tools through
the real ``ToolRegistry.invoke``, exactly as the deep worker's shell command does.
"""
import tempfile

from hubofhubs.act.tool_ledger import (
    FileToolLedger, ToolOutcome, failure_signature, repeated_failure, unrecovered_failures)
from hubofhubs.ports.deep import DeepResult
from quest_ai_runner.core.orchestrator import OrchestratorResult, PlanDecision
from quest_ai_runner.core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.runner.executor import TaskExecutor

from .conftest import StubProvider
from .test_fast_edit_ladder import _orch

TASK = "task_1"


def failing_read(message):
    return ToolSpec(name="read_things", description="reads", mutates=False,
                    handler=lambda args, ctx: ToolResult(ok=False, text=message))


def working_write():
    return ToolSpec(name="write_things", description="writes", mutates=True,
                    handler=lambda args, ctx: ToolResult(ok=True, text="written"))


class ToolCallingRunner:
    """A deep worker that, each attempt, makes the scripted tool calls and answers with ``output``."""

    def __init__(self, registry, scripts, output="could not finish"):
        self.registry, self.scripts, self.output = registry, list(scripts), output
        self.calls = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, **kwargs):
        self.calls.append({"model": model})
        for tool in (self.scripts.pop(0) if self.scripts else []):
            self.registry.invoke(tool, {}, ToolContext(task_id=TASK))
        return DeepResult(met=False, output=self.output)


def mislabelled(reason="the goals still have to be read"):
    return {"met": False, "reason": reason, "blocker": "more_work", "next_tier": "best"}


def setup(tmp_path, specs, scripts, verdicts, output="could not finish"):
    registry = ToolRegistry(specs, ledger=FileToolLedger(str(tmp_path)))
    runner = ToolCallingRunner(registry, scripts, output)
    orch = _orch(StubProvider([]), [runner], verdicts)
    orch.tools = registry
    return orch, runner


def run_deep(orch):
    return orch._run_deep(PlanDecision(action="deep", goal="g", deep_brief="b"), "g", "sonnet",
                          ctx_meta={"task_id": TASK})


SERVER_ERROR = "The goals could not be read just now (Quest API answered 500: validation error)."


def test_a_more_work_mislabel_still_stops_after_two_attempts_on_one_model(tmp_path):
    orch, runner = setup(tmp_path, [failing_read(SERVER_ERROR)], [["read_things"]] * 8,
                         [mislabelled(f"reading goals failed, attempt {n}") for n in range(8)])
    done = run_deep(orch).deep_results[0]
    assert len(runner.calls) == 2
    assert len({c["model"] for c in runner.calls}) == 1
    assert not done.met and "read_things" in done.tool_failure and "500" in done.tool_failure


def test_the_mislabelled_task_ends_incomplete_naming_the_tool(tmp_path):
    orch, _ = setup(tmp_path, [failing_read(SERVER_ERROR)], [["read_things"]] * 8,
                    [mislabelled() for _ in range(8)])
    results = run_deep(orch).deep_results
    ex = TaskExecutor(_Client(), None)
    ex._post_conv = lambda *a, **k: None
    ex._report_progress = lambda *a, **k: None
    ex._with_context_receipt = lambda text, *a, **k: text
    out = ex._report("t1", OrchestratorResult(kind="deep", deep_results=results, goals=["g"]))
    assert out.status == "incomplete"
    assert "read_things" in out.result and "500" in out.result and "—" not in out.result


def test_a_failure_the_worker_recovered_from_is_not_a_broken_tool(tmp_path):
    flaky = {"n": 0}

    def handler(args, ctx):
        flaky["n"] += 1
        return ToolResult(ok=flaky["n"] % 2 == 0, text=SERVER_ERROR)   # fails, then works

    spec = ToolSpec(name="read_things", description="reads", mutates=False, handler=handler)
    orch, runner = setup(tmp_path, [spec], [["read_things", "read_things"]] * 3,
                         [mislabelled("a different gap, wording one"),
                          mislabelled("another gap entirely, wording two"), {"met": True}])
    assert run_deep(orch).deep_results[0].met and len(runner.calls) == 3


def test_a_failure_that_changes_between_attempts_is_not_the_same_failure(tmp_path):
    state = {"n": 0}

    def handler(args, ctx):
        state["n"] += 1
        return ToolResult(ok=False, text="denied (403)" if state["n"] == 1 else SERVER_ERROR)

    spec = ToolSpec(name="read_things", description="reads", mutates=False, handler=handler)
    orch, runner = setup(tmp_path, [spec], [["read_things"]] * 3,
                         [mislabelled("gap one is about access"), mislabelled("gap two differs"),
                          {"met": True}])
    run_deep(orch)
    assert len(runner.calls) == 3


def test_the_same_verdict_and_output_with_nothing_changed_stops_at_two_attempts(tmp_path):
    orch, runner = setup(tmp_path, [working_write()], [[]] * 8,
                         [mislabelled("the section on pricing is still missing") for _ in range(8)],
                         output="The pricing section is not written yet.")
    done = run_deep(orch).deep_results[0]
    # Attempt one cannot know it is stuck, so its normal retry may use a stronger model; what the
    # guard stops is a third attempt (and the escalation after it).
    assert len(runner.calls) == 2
    assert "pricing" in done.stalled and not done.tool_failure


def test_an_attempt_that_wrote_something_is_progress_even_if_the_verdict_repeats(tmp_path):
    orch, runner = setup(tmp_path, [working_write()], [["write_things"]] * 3,
                         [mislabelled("the section on pricing is still missing"),
                          mislabelled("the section on pricing is still missing"), {"met": True}],
                         output="The pricing section is not written yet.")
    assert run_deep(orch).deep_results[0].met and len(runner.calls) == 3


def test_a_stalled_task_ends_incomplete_with_a_plain_message(tmp_path):
    orch, _ = setup(tmp_path, [working_write()], [[]] * 8,
                    [mislabelled("the section on pricing is still missing") for _ in range(8)],
                    output="The pricing section is not written yet.")
    results = run_deep(orch).deep_results
    ex = TaskExecutor(_Client(), None)
    ex._post_conv = lambda *a, **k: None
    ex._report_progress = lambda *a, **k: None
    ex._with_context_receipt = lambda text, *a, **k: text
    out = ex._report("t1", OrchestratorResult(kind="deep", deep_results=results, goals=["g"]))
    assert out.status == "incomplete" and "pricing" in out.result and "—" not in out.result


def test_signature_ignores_ids_and_numbers_but_keeps_the_status():
    a = failure_signature("t", "Goals of quest quest_0123456789ab failed: Quest API -> 500 after 12s")
    b = failure_signature("t", "Goals of quest quest_ba9876543210 failed: Quest API -> 500 after 3s")
    c = failure_signature("t", "Goals of quest quest_0123456789ab failed: Quest API -> 403 after 12s")
    assert a == b and a != c


def test_two_processes_share_one_ledger_by_task_id():
    with tempfile.TemporaryDirectory() as d:
        brain = ToolRegistry([failing_read(SERVER_ERROR)], ledger=FileToolLedger(d))
        shell = ToolRegistry([failing_read(SERVER_ERROR)], ledger=FileToolLedger(d))
        mark = brain.ledger.mark(TASK)
        shell.invoke("read_things", {}, ToolContext(task_id=TASK))
        window = brain.ledger.since(TASK, mark)
        assert [c.tool for c in window] == ["read_things"] and not window[0].ok
        assert brain.ledger.mark("other_task") == 0


def test_repeated_failure_needs_the_same_signature_in_both_windows():
    one = ToolOutcome(tool="t", ok=False, signature="t|500|x")
    two = ToolOutcome(tool="t", ok=False, signature="t|403|x")
    assert repeated_failure(unrecovered_failures([one]), unrecovered_failures([one])) is one
    assert repeated_failure(unrecovered_failures([one]), unrecovered_failures([two])) is None


class _Client:
    def report_incomplete(self, task_id, result, session_id=None):
        pass
