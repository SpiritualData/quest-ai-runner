"""A not-met verdict says WHY another attempt would or would not help, and the loop and report act on it.

The verifier returns a ``blocker`` in the same call it already makes (no extra LLM call):
``more_work`` retries as before, ``evidence_only`` is accepted with the unconfirmed part named
(re-running the worker to re-prove finished work is pure cost), and ``needs_person`` stops at once
and asks the one question. Both the task modal and chat tasks end in ``TaskExecutor._report``.
"""
from quest_ai_runner.core import orchestrator as o
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorResult, PlanDecision
from quest_ai_runner.runner.executor import TaskExecutor

from .conftest import StubProvider, StubRetrieval
from .test_fast_edit_ladder import RecordingRunner, _orch


def run_with_verdicts(verdicts, runner=None):
    runner = runner or RecordingRunner(DeepResult(met=True, output="Implemented it in commit abc123."))
    orch = _orch(StubProvider([]), [runner], verdicts)
    res = orch._run_deep(PlanDecision(action="deep", goal="g", deep_brief="b"), "g", "sonnet")
    return runner, res.deep_results[0]


def test_schema_and_prompt_carry_the_blocker():
    props = o.VERIFY_GOAL_TOOL["input_schema"]["properties"]
    assert props["blocker"]["enum"] == ["more_work", "evidence_only", "needs_person"]
    assert "question" in props
    t = " ".join(o.VERIFY_GOAL_PROMPT.split())
    assert "needs_person" in t and "never to hand the worker's own job to a person" in t


def test_more_work_still_retries():
    runner, d = run_with_verdicts([{"met": False, "reason": "half done", "blocker": "more_work"}, {"met": True}])
    assert len(runner.calls) == 2 and d.met


def test_evidence_only_is_accepted_without_a_retry_and_names_the_gap():
    runner, d = run_with_verdicts([{"met": False, "reason": "no test log", "blocker": "evidence_only",
                       "question": "a passing test run"}])
    assert len(runner.calls) == 1
    assert d.met is True and d.error is None
    assert d.unconfirmed_note == "a passing test run"


def test_evidence_only_is_not_trusted_when_a_claimed_change_is_unbacked():
    runner, d = run_with_verdicts([{"met": False, "reason": "claims a save", "blocker": "evidence_only",
                       "question": "x", "claims_unexecuted": True},
                      {"met": True}])
    assert len(runner.calls) == 2


def test_needs_person_stops_at_once_with_the_question():
    runner, d = run_with_verdicts([{"met": False, "reason": "needs the account", "blocker": "needs_person",
                       "question": "Which Stripe account should I use?"}])
    assert len(runner.calls) == 1
    assert not d.met and d.needs_person == "Which Stripe account should I use?"


def test_needs_person_without_a_question_is_just_more_work():
    runner, d = run_with_verdicts([{"met": False, "reason": "r", "blocker": "needs_person", "question": ""},
                      {"met": True}])
    assert len(runner.calls) == 2


# --- the report ---------------------------------------------------------------------------

class Client:
    def __init__(self):
        self.reports = []

    def report_done(self, task_id, result, session_id=None):
        self.reports.append(("done", result))

    def report_needs_you(self, task_id, result, decision_id, session_id=None):
        self.reports.append(("needs_you", result))

    def report_incomplete(self, task_id, result, session_id=None):
        self.reports.append(("incomplete", result))

    def report_failed(self, task_id, result, session_id=None):
        self.reports.append(("failed", result))


def report_for(deep):
    ex = TaskExecutor(Client(), None)
    ex._post_conv = lambda *a, **k: None
    ex._report_progress = lambda *a, **k: None
    ex._compose_done_report = lambda req, summary, *a, **k: summary
    ex._with_context_receipt = lambda text, *a, **k: text
    return ex._report("t1", OrchestratorResult(kind="deep", deep_results=deep, goals=["g"]))


def test_needs_person_reports_needs_you_with_the_question():
    out = report_for([DeepResult(met=False, output="did most of it", needs_person="Which account?")])
    assert out.status == "needs_you" and "Which account?" in out.result


def test_unconfirmed_note_rides_the_done_report():
    out = report_for([DeepResult(met=True, output="Done in abc123", unconfirmed_note="the deploy")])
    assert out.status == "done" and "Not independently confirmed: the deploy" in out.result


def test_terminal_not_met_leads_with_what_is_left_and_keeps_the_marker():
    out = report_for([DeepResult(met=False, output="edited two files", error="Goal not yet met: x",
                              verdict_reason="the client is not updated",
                              verdict_next_action="update the client call")])
    assert out.status == "incomplete"
    assert out.result.startswith("I got part of the way. What is still open: the client is not updated")
    assert "Next step: update the client call" in out.result
    assert "same session" in out.result
    assert "--- WHAT THE RUN DID BEFORE IT STOPPED (unfinished, not verified) ---" in out.result


def test_terminal_not_met_without_a_verdict_keeps_the_old_wording():
    out = report_for([DeepResult(met=False, output="edited", error="the turn budget ran out")])
    assert out.result.startswith("the turn budget ran out")
