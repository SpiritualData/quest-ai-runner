"""A finished task reports the run's metric facts to the Quest backend, as data the loop kept and not as words.

``OrchestratorResult.metric_results`` / ``.metric_missing`` (written by the goal loop from its metric ledger,
``docs/metric-results.md``) ride in ``result_data`` on every terminal report the executor makes, and a milestone that
carries them passes them on as the progress post's ``data``. A client or a test double that does not declare
``result_data`` is called exactly as before, and a run with no metric facts reports exactly as before.
"""
from hubofhubs.ports.deep import DeepResult, ProgressEvent
from esoi.metric_results import MAX_METRICS, MetricResult, payload_of, reconcile
from quest_ai_runner.core.orchestrator_types import OrchestratorResult
from quest_ai_runner.runner.executor import TaskExecutor
from quest_ai_runner.runner.executor_support import _TaskProgressSink
from quest_ai_runner.runner.quest_client import QuestClient
from quest_ai_runner.runner.quest_client_support import QuestApiError

ENTRIES = payload_of(reconcile([
    MetricResult(source="measured", solution_id="receive-input", metric_id="metric-receive-input",
                 name="Exactly-once delivery per run", milestone="Delivery recorded", before=0.5, after=0.75,
                 reason="measured by offline_arm"),
    MetricResult(source="not_applicable", reason="Drafted text, nothing to measure."),
]))


class LaneClient:
    """A current client: every terminal report declares ``session_id`` and ``result_data``."""

    def __init__(self):
        self.reports = []
        self.progress = []

    def report_done(self, task_id, result, session_id=None, result_data=None):
        self.reports.append(("done", result, result_data))

    def report_incomplete(self, task_id, result, session_id=None, result_data=None):
        self.reports.append(("incomplete", result, result_data))

    def report_failed(self, task_id, result, session_id=None, result_data=None):
        self.reports.append(("failed", result, result_data))

    def report_needs_you(self, task_id, result, decision_id, session_id=None, result_data=None):
        self.reports.append(("needs_you", result, result_data))

    def report_progress(self, task_id, kind, *, text=None, output=None, data=None):
        self.progress.append((kind, text, data))


class OlderClient:
    """A client written before ``result_data`` and ``session_id`` existed on the terminal reports."""

    def __init__(self):
        self.reports = []

    def report_done(self, task_id, result):
        self.reports.append(("done", result))

    def report_incomplete(self, task_id, result):
        self.reports.append(("incomplete", result))

    def report_failed(self, task_id, result):
        self.reports.append(("failed", result))

    def report_needs_you(self, task_id, result, decision_id):
        self.reports.append(("needs_you", result))


def report(client, result):
    executor = TaskExecutor(client, None)
    executor._post_conv = lambda *a, **k: None
    executor._report_progress = lambda *a, **k: None
    executor._with_context_receipt = lambda text, *a, **k: text
    executor._report("t1", result)
    return client.reports[-1]


def deep(*, met, metrics=ENTRIES, missing=False, **kw):
    """A deep turn whose goal loop already kept the metric record on the turn's result."""
    return OrchestratorResult(kind="deep", deep_results=[DeepResult(met=met, **kw)], goals=["g"],
                              metric_results=list(metrics), metric_missing=missing)


def test_a_done_report_carries_the_metric_entries_next_to_the_outcome():
    status, _, data = report(LaneClient(), deep(met=True, output="Finished the work."))
    assert status == "done"
    assert data == {"outcome": "done", "metrics": ENTRIES}
    assert data["metrics"] == ENTRIES and data["metrics"] is not ENTRIES


def test_metric_missing_is_reported_only_when_it_is_true():
    _, _, data = report(LaneClient(), deep(met=True, output="Done.", metrics=[], missing=True))
    assert data == {"outcome": "done", "metric_missing": True}
    _, _, data = report(LaneClient(), deep(met=True, output="Done.", metrics=ENTRIES, missing=False))
    assert "metric_missing" not in data


def test_every_other_terminal_status_carries_them_too():
    status, _, data = report(LaneClient(), deep(met=False, output="Part of it is done.", error="goal not met"))
    assert (status, data) == ("incomplete", {"metrics": ENTRIES})
    status, _, data = report(LaneClient(), deep(met=False, output="", error="the worker crashed"))
    assert (status, data) == ("failed", {"metrics": ENTRIES})
    status, _, data = report(LaneClient(), deep(met=False, output="", decision_id="dec_1", missing=True))
    assert (status, data) == ("needs_you", {"metrics": ENTRIES, "metric_missing": True})
    confirm = OrchestratorResult(kind="confirm", question="Shall I send it?", decision_id="dec_2",
                                 metric_results=list(ENTRIES))
    status, _, data = report(LaneClient(), confirm)
    assert (status, data) == ("needs_you", {"metrics": ENTRIES})


def test_an_answer_turn_that_ran_deep_work_carries_them():
    answer = OrchestratorResult(kind="answer", text="All set.", metric_results=list(ENTRIES))
    status, _, data = report(LaneClient(), answer)
    assert (status, data) == ("done", {"outcome": "nothing_done", "metrics": ENTRIES})


def test_a_run_with_no_metric_facts_reports_exactly_as_before():
    status, _, data = report(LaneClient(), deep(met=True, output="Done.", metrics=[]))
    assert (status, data) == ("done", {"outcome": "done"})
    for result in (deep(met=False, output="Part.", error="goal not met", metrics=[]),
                   deep(met=False, output="", error="boom", metrics=[]),
                   deep(met=False, output="", decision_id="dec_3", metrics=[])):
        status, _, data = report(LaneClient(), result)
        assert data is None, status


def test_facts_kept_only_on_the_deep_results_are_read_the_way_the_loop_reads_them():
    """An integrator that builds the result for ``report()`` may fill the deep results and nothing else."""
    result = OrchestratorResult(kind="deep", goals=["g"], deep_results=[
        DeepResult(met=True, output="Done.", metric_results=list(ENTRIES), metric_missing=True)])
    _, _, data = report(LaneClient(), result)
    assert data == {"outcome": "done", "metrics": ENTRIES, "metric_missing": True}


def test_what_is_reported_is_capped_and_only_dict_entries_count():
    many = [dict(ENTRIES[0], metric_id=f"m{i}") for i in range(MAX_METRICS + 5)]
    _, _, data = report(LaneClient(), deep(met=True, output="Done.", metrics=many + ["not an entry", 3]))
    assert data["metrics"] == many[:MAX_METRICS]


def test_an_older_client_is_called_exactly_as_it_always_was():
    client = OlderClient()
    for result in (deep(met=True, output="Done."),
                   deep(met=False, output="Part.", error="goal not met"),
                   deep(met=False, output="", error="boom"),
                   deep(met=False, output="", decision_id="dec_4")):
        report(client, result)
    assert [r[0] for r in client.reports] == ["done", "incomplete", "failed", "needs_you"]
    assert client.reports[0] == ("done", "Done.")


# --- the real client's wire ---------------------------------------------------------------------


def wire_bodies(monkeypatch, refuse_result_data=False):
    bodies = []

    def fake_request(self, method, path, body=None, **kw):
        bodies.append(dict(body))
        if refuse_result_data and "result_data" in body:
            raise QuestApiError("PATCH -> 422: result_data: Extra inputs are not permitted", status=422)
        return {"status": body["status"]}

    monkeypatch.setattr(QuestClient, "_request", fake_request)
    return bodies


def test_the_real_client_sends_result_data_on_every_terminal_report(monkeypatch):
    bodies = wire_bodies(monkeypatch)
    client = QuestClient(base_url="http://x", api_key="k")
    facts = {"metrics": ENTRIES, "metric_missing": True}
    client.report_done("t1", "ok", session_id="s1", result_data={"outcome": "done", **facts})
    client.report_incomplete("t1", "part", result_data=facts)
    client.report_failed("t1", "boom", result_data=facts)
    client.report_needs_you("t1", "ask", "dec_1", result_data=facts)
    assert [b["status"] for b in bodies] == ["done", "incomplete", "failed", "needs_you"]
    assert bodies[0]["result_data"] == {"outcome": "done", **facts} and bodies[0]["session_id"] == "s1"
    assert all(b["result_data"] == facts for b in bodies[1:])
    assert bodies[3]["decision_id"] == "dec_1"


def test_the_real_client_sends_no_result_data_key_when_there_is_none(monkeypatch):
    bodies = wire_bodies(monkeypatch)
    client = QuestClient(base_url="http://x", api_key="k")
    client.report_incomplete("t1", "part")
    client.report_failed("t1", "boom", result_data={})
    client.report_needs_you("t1", "ask", "dec_1", session_id="s9")
    assert bodies == [{"status": "incomplete", "result": "part"}, {"status": "failed", "result": "boom"},
                      {"status": "needs_you", "result": "ask", "decision_id": "dec_1", "session_id": "s9"}]


def test_a_backend_that_refuses_result_data_still_gets_every_status_reported(monkeypatch):
    bodies = wire_bodies(monkeypatch, refuse_result_data=True)
    client = QuestClient(base_url="http://x", api_key="k")
    facts = {"metrics": ENTRIES}
    assert client.report_incomplete("t1", "part", result_data=facts) == {"status": "incomplete"}
    assert client.report_failed("t1", "boom", result_data=facts) == {"status": "failed"}
    assert client.report_needs_you("t1", "ask", "dec_1", result_data=facts) == {"status": "needs_you"}
    assert [("result_data" in b) for b in bodies] == [True, False, True, False, True, False]


# --- the live task feed -------------------------------------------------------------------------


def milestone(text, data=None):
    return ProgressEvent(type="milestone", text=text, data=data)


def feed(event):
    client = LaneClient()
    executor = TaskExecutor(client, None)
    executor._on_milestone("t1", None, event)
    return client.progress


def test_a_milestone_with_the_metric_result_passes_exactly_those_facts_on():
    event = milestone("Completed: the goal", {"metrics": ENTRIES, "metric_missing": True, "goal": "g", "n": 3})
    assert feed(event) == [("exec", "Completed: the goal", {"metrics": ENTRIES, "metric_missing": True})]


def test_a_milestone_without_them_is_posted_as_before():
    assert feed(milestone("Retrying with a stronger model")) == [("exec", "Retrying with a stronger model", None)]
    assert feed(milestone("Worker output", {"attempt": 2})) == [("exec", "Worker output", None)]
    assert feed(milestone("Completed: x", {"metrics": [], "metric_missing": False})) == [("exec", "Completed: x", None)]
    assert feed(milestone("Completed: x", {"metrics": "0.5 -> 0.7"})) == [("exec", "Completed: x", None)]


def test_the_progress_sink_still_forwards_every_event_and_the_exec_post_adds_the_facts():
    client = LaneClient()
    executor = TaskExecutor(client, None)
    sink = _TaskProgressSink("t1", executor._report_progress,
                             on_milestone=lambda ev: executor._on_milestone("t1", None, ev))
    event = milestone("Completed: the goal", {"metrics": ENTRIES})
    sink.update(event, None)
    sink.update(ProgressEvent(type="partial", text="streaming", data=None), None)
    assert client.progress == [("milestone", "Completed: the goal", {"metrics": ENTRIES}),
                               ("exec", "Completed: the goal", {"metrics": ENTRIES})]
