"""A task that reaches ``done`` says whether it did any work: ``result_data.outcome`` is ``done`` when
the run made a successful mutating call (or a deep goal was verified met) and ``nothing_done`` for an
answer that only talked. Quest Workflows reads it so a step is not recorded done on a reply like
"I can send it if you'd like"."""
from hubofhubs.act.execution_record import ExecutionFact, ExecutionRecord
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


def test_a_write_held_for_approval_is_work_done():
    """A requested draft or proposal that Quest took and holds for a person did its work: it is not stamped nothing_done."""
    record = ExecutionRecord(facts=[ExecutionFact(goal="tool propose_value: raised for approval", phases=["tool_call", "held"])])
    _, data = run(OrchestratorResult(kind="answer", text="Proposed; waiting for you.", execution_record=record))
    assert data == {"outcome": "done"}


def test_a_failed_mutating_call_is_not_work_done():
    record = ExecutionRecord(facts=[ExecutionFact(goal="tool send_message", failed=True)])
    _, data = run(OrchestratorResult(kind="answer", text="It failed.", execution_record=record))
    assert data == {"outcome": "nothing_done"}


def test_a_backend_that_refuses_result_data_still_gets_the_task_reported_done(monkeypatch):
    """A backend older than ``result_data`` answers 422 to the whole PATCH; the report is retried
    without the stamp instead of leaving the finished task in progress."""
    from quest_ai_runner.runner.quest_client import QuestClient
    from quest_ai_runner.runner.quest_client_support import QuestApiError

    bodies = []

    def fake_request(self, method, path, body=None, **kw):
        bodies.append(dict(body))
        if "result_data" in body:
            raise QuestApiError("PATCH -> 422: result_data: Extra inputs are not permitted", status=422)
        return {"status": "done"}

    monkeypatch.setattr(QuestClient, "_request", fake_request)
    client = QuestClient(base_url="http://x", api_key="k")
    out = client.report_done("t1", "ok", result_data={"outcome": "done"})
    assert out == {"status": "done"} and "result_data" in bodies[0] and "result_data" not in bodies[1]
