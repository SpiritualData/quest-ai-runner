"""The after of a milestone deliverable made as background work: when its queued task finishes, code measures the deliverable against its rubric (the
turn's separate scorer) and keeps it on the milestone's keyed goal with the increment before and after."""
import datetime
import json
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_completion as mc

RECORD = {"quest_id": "q-1", "milestone_id": "sol-1", "milestone_metric_id": "met-1", "title": "M1 Tutorials published deliverables", "item": "tutorial scripts",
          "requirements": ["Has an opening", "Ends with a call to action"], "before": 0, "total": 6, "measure_name": "Tutorial scripts meeting the rubric",
          "metric_id": "met-1"}


class Registry:
    def __init__(self, kept=""):
        self.kept, self.calls = kept, []

    def get(self, name):
        return name

    def invoke(self, name, args, ctx=None):
        self.calls.append((name, args))
        if name == "read_spec_records":
            return SimpleNamespace(ok=True, data={"records": {"metrics": {"met-1": {"name": "Tutorials published", "record": "Current value: 0 of 6"}}}}, text="records")
        if name == "list_quest_goals":
            return SimpleNamespace(ok=True, data=[{"title": RECORD["title"], "text": self.kept, "spec_solution_id": "sol-1"}], text="goals")
        return SimpleNamespace(ok=True, data={"state": "updated"}, text="kept")


def scorer(prompt):
    text = prompt.split("The text:", 1)[1].split("Requirements:", 1)[0]
    return json.dumps({"verdicts": [{"n": n, "meets": q in text, "quote": q if q in text else ""} for n, q in enumerate(("Opening line", "Call to action"), 1)]})


def test_the_work_record_is_one_code_written_line_read_back_whole():
    brief = "Make it.\n" + mc.work_record_line(RECORD) + "\nMore text."
    assert mc.work_record_of(brief) == RECORD and mc.work_record_of("no record here") is None


def test_a_finished_background_deliverable_is_measured_and_kept_with_before_and_after():
    registry = Registry()
    output = "I researched and wrote it.\n\nDELIVERABLE:\nOpening line for tutorial 1. Teach the product. Call to action: try it today."
    got = mc.measure_on_completion(RECORD, output, registry, scorer, today=datetime.date(2030, 3, 9))
    assert got["status"] == "measured" and (got["before"], got["after"]) == (0, 1)
    assert got["line"].startswith("Tutorial scripts meeting the rubric: 0 before, 1 after of 6 planned")
    assert any(n == "keep_milestone_deliverable" and a["increment"]["after"] == 1 and "Opening line for tutorial 1" in a["body"] for n, a in registry.calls)
    assert got["milestone_metric"] == {"id": "met-1", "record": "Current value: 0 of 6"} and got["increment"]["method"] == "rubric"


def test_a_deliverable_the_worker_already_kept_is_measured_not_added_twice_and_no_deliverable_keeps_nothing():
    unit = "Opening line for tutorial 1. Call to action: try it today."
    registry = Registry(kept="Kept by Quest AI: Overview:\nTutorials.\n\n--- Part 1 ---\n" + unit)
    got = mc.measure_on_completion(RECORD, "DELIVERABLE:\n" + unit, registry, scorer)
    kept = next(a for n, a in registry.calls if n == "keep_milestone_deliverable")
    assert got["after"] == 1 and kept["body"].count(unit) == 1
    empty = Registry()
    assert mc.measure_on_completion(RECORD, "I could not finish.", empty, scorer)["status"] == "no_deliverable" and not any(n == "keep_milestone_deliverable" for n, _ in empty.calls)


def test_the_task_executor_measures_a_finished_milestone_task_and_adds_the_line_to_its_result():
    from quest_ai_runner.the_consciousness.request_intake.executor import TaskExecutor
    registry = Registry()
    holder = SimpleNamespace(_orch=SimpleNamespace(tools=registry, cheap_decision=scorer))
    result = SimpleNamespace(text="Done.", deep_results=[SimpleNamespace(output="DELIVERABLE:\nOpening line. Call to action: go.")])
    TaskExecutor.measure_milestone_work(holder, "Brief.\n" + mc.work_record_line(RECORD), result)
    assert "0 before, 1 after of 6 planned" in result.text
    plain = SimpleNamespace(text="Done.", deep_results=[])
    TaskExecutor.measure_milestone_work(holder, "an ordinary task", plain)
    assert plain.text == "Done."
