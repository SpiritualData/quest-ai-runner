"""A tool the work needs keeps failing: the goal loop stops after two attempts, on one model, and
the task ends incomplete naming the failing tool and its error instead of looping and billing.

Reproduces the 2026-10-16 incident: a goals read answering a server error sent one task through five
fresh worker sessions, each judged "not met" and each on a stronger model, until a person cancelled it.
The verifier is scripted (an LLM call); the goal loop and the report are the real ones.
"""
from quest_ai_runner.core import orchestrator as o
from hubofhubs.ports.deep import DeepResult
from quest_ai_runner.core.orchestrator import PlanDecision
from quest_ai_runner.runner.executor import TaskExecutor
from quest_ai_runner.core.orchestrator import OrchestratorResult

from .conftest import StubProvider
from .test_fast_edit_ladder import RecordingRunner, _orch

BROKEN = DeepResult(met=False, output="list_quest_goals answered 500 (Goal id Field required); I could not read the goals.")
FAILURE = {"met": False, "reason": "the goals could not be read", "blocker": "tool_failure",
           "question": "The list_quest_goals tool answered a server error 500 (a goal record has no id).",
           "next_tier": "best"}


def run(verdicts, runner=None, cancel_check=None):
    runner = runner or RecordingRunner(BROKEN)
    orch = _orch(StubProvider([]), [runner], verdicts)
    res = orch._run_deep(PlanDecision(action="deep", goal="g", deep_brief="b"), "g", "sonnet",
                         cancel_check=cancel_check)
    return runner, res


def test_repeated_tool_failure_stops_after_two_attempts_on_one_model():
    runner, res = run([dict(FAILURE) for _ in range(10)])
    done = res.deep_results[0]
    assert len(runner.calls) == 2
    assert len({c["model"] for c in runner.calls}) == 1, "a broken tool is not a capability gap: no escalation"
    assert not done.met and "list_quest_goals" in done.tool_failure and "500" in done.tool_failure


def test_a_transient_failure_gets_its_one_retry():
    runner, res = run([dict(FAILURE), {"met": True}])
    assert len(runner.calls) == 2 and res.deep_results[0].met


def test_schema_and_prompt_carry_the_tool_failure_blocker():
    assert "tool_failure" in o.VERIFY_GOAL_TOOL["input_schema"]["properties"]["blocker"]["enum"]
    assert "tool_failure" in " ".join(o.VERIFY_GOAL_PROMPT.split())


def test_the_task_ends_incomplete_naming_the_failing_tool_and_error():
    _, res = run([dict(FAILURE) for _ in range(10)])
    ex = TaskExecutor(_Client(), None)
    ex._post_conv = lambda *a, **k: None
    ex._report_progress = lambda *a, **k: None
    ex._with_context_receipt = lambda text, *a, **k: text
    out = ex._report("t1", OrchestratorResult(kind="deep", deep_results=res.deep_results, goals=["g"]))
    assert out.status == "incomplete"
    assert "list_quest_goals" in out.result and "500" in out.result and "not change" in out.result
    assert "—" not in out.result


def test_a_task_cancelled_during_an_attempt_stops_before_verifying_or_retrying():
    verified = []
    runner = RecordingRunner(BROKEN)
    orch = _orch(StubProvider([]), [runner], [])
    orch._verify_goal = lambda *a, **k: (verified.append(1) or {"met": False, "reason": "x"}, None)
    res = orch._run_deep(PlanDecision(action="deep", goal="g", deep_brief="b"), "g", "sonnet",
                         cancel_check=lambda: len(runner.calls) >= 1)
    assert len(runner.calls) == 1 and not verified and res.kind == "cancelled"


class _Client:
    def report_incomplete(self, task_id, result, session_id=None):
        pass
