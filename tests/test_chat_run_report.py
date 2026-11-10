"""What a person reads from a run started in their chat: a report written for them, a plain line for what the run is doing, no model name in the text, and a
hand-off that says it is still working (never that it stopped). Each is decided from structured facts: where the request came from, the event's tool, the result's exit reason."""
from types import SimpleNamespace

from hubofhubs.api import DeepResult, ProgressEvent

from quest_ai_runner.the_body.internal_context.run_store.executor_reporting import ReportingMixin
from quest_ai_runner.the_consciousness.request_intake.executor_support import _model_used_note, run_description
from quest_ai_runner.the_intelligence.mind.delegation.run_facts import end_report_data
from quest_ai_runner.the_intelligence.mind.response_writer.answer_explanation import REPORT_STYLE_PERSON
from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import turn_outcome


def test_what_a_run_is_doing_is_a_plain_line_never_a_command_or_a_host():
    raw = ProgressEvent(type="exec", text="$ python3 quest.py add_quest_note --kw 'reason=Adds a research note only' 2>&1 | tee run.log", data={"phase": "tool_call"})
    line = run_description(raw, "Research typical costs for retiling a floor, then add a note to this quest.")
    assert line.startswith("Working on: Research typical costs") and "$" not in line and "--kw" not in line
    named = ProgressEvent(type="exec", text="tool add_quest_note", data={"phase": "tool_call", "tool": "add_quest_note"})
    assert run_description(named, "x") == "Adding a note to the quest"
    assert run_description(ProgressEvent(type="token", text="t", data={}), "x") == ""


class Orchestrator:
    def __init__(self):
        self.styles = []

    def synthesize_task_report(self, request, summary, rep_preamble=None, report_style=""):
        self.styles.append(report_style)
        return "Added a note to your quest with a short budget list and the three first steps."

    def report_claims_unbacked(self, *a, **k):
        return False


class Legacy:
    def synthesize_task_report(self, request, summary, rep_preamble=None):      # a brain that has not heard of report styles is called as before
        return "ok report"

    def report_claims_unbacked(self, *a, **k):
        return False


def reporter(orch):
    r = ReportingMixin.__new__(ReportingMixin)
    r._orch = orch
    return r


def test_a_run_started_in_a_chat_is_reported_for_the_person_and_any_other_run_in_the_engineering_shape():
    orch = Orchestrator()
    result = SimpleNamespace(execution_record=None)
    reporter(orch)._compose_done_report("add a note", "raw output", result, None, for_person=True)
    reporter(orch)._compose_done_report("add a note", "raw output", result, None, for_person=False)
    assert orch.styles == [REPORT_STYLE_PERSON, ""]
    assert reporter(Legacy())._compose_done_report("add a note", "raw output", result, None, for_person=True) == "ok report"


def test_the_model_that_ran_the_work_is_in_the_run_record_not_in_the_report():
    d = DeepResult(met=True, model="sonnet", resolved_model="claude-x")
    assert "Completed with model" in _model_used_note([d])      # still produced, for the task's own stream
    import inspect
    from quest_ai_runner.the_body.internal_context.run_store import executor_reporting
    assert "done_report += _model_used_note" not in inspect.getsource(executor_reporting)


def test_a_turn_that_handed_the_rest_to_a_longer_run_ends_as_a_hand_off_not_as_a_stop():
    handed = SimpleNamespace(deep_results=[], exit_reason="deferred", goal_verdict={"met": False, "reason": "the note is not added"},
                             handed_off_goal="Research costs and add the note")
    assert turn_outcome(handed) == {"outcome": "met"}
    assert end_report_data(handed) == {"end_reason": "handed_off", "left": "Research costs and add the note"}
    capped = SimpleNamespace(deep_results=[], exit_reason="max_turns", goal_verdict={"met": False, "reason": "the note is not added"})
    assert end_report_data(capped)["end_reason"] == "attempt_cap"
