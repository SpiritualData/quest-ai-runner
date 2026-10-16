"""A task that reaches ``done`` says whether it did any work: ``result_data.outcome`` is ``done`` when
the run made a successful mutating call (or a deep goal was verified met) and ``nothing_done`` for an
answer that only talked. Quest Workflows reads it so a step is not recorded done on a reply like
"I can send it if you'd like"."""
from quest_ai_runner.core.guard import ExecutionFact, ExecutionRecord
from quest_ai_runner.core.orchestrator_types import OrchestratorResult
from quest_ai_runner.runner.executor import TaskExecutor


class Client:
    def __init__(self):
        self.done = []

    def report_done(self, task_id, result, session_id=None, result_data=None):
        self.done.append((result, result_data))


def run(result):
    client = Client()
    ex = TaskExecutor(client, None)
    ex._post_conv = lambda *a, **k: None
    ex._report_progress = lambda *a, **k: None
    ex._with_context_receipt = lambda text, *a, **k: text
    ex._report("t1", result)
    return client.done[-1]


def test_an_answer_with_a_successful_mutating_call_is_done():
    record = ExecutionRecord(facts=[ExecutionFact(goal="tool send_message", succeeded=True)])
    _, data = run(OrchestratorResult(kind="answer", text="Sent.", execution_record=record))
    assert data == {"outcome": "done"}


def test_an_answer_that_only_talked_is_nothing_done():
    _, data = run(OrchestratorResult(kind="answer", text="Nothing has been sent. I can send it if you'd like.", execution_record=ExecutionRecord()))
    assert data == {"outcome": "nothing_done"}
    _, data = run(OrchestratorResult(kind="answer", text="No record at all"))
    assert data == {"outcome": "nothing_done"}


def test_a_failed_mutating_call_is_not_work_done():
    record = ExecutionRecord(facts=[ExecutionFact(goal="tool send_message", failed=True)])
    _, data = run(OrchestratorResult(kind="answer", text="It failed.", execution_record=record))
    assert data == {"outcome": "nothing_done"}
