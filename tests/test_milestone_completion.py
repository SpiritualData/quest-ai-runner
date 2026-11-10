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


def test_a_whole_kept_body_the_worker_wrote_is_measured_part_by_part():
    registry = Registry()
    body = "Kept by Quest AI: Overview:\nTutorials.\n\n--- Part 1 ---\nOpening line one. Call to action: go.\n\n--- Part 2 ---\nOpening line two. Call to action: try."
    got = mc.measure_on_completion(RECORD, "DELIVERABLE:\n" + body, registry, scorer)
    assert got["after"] == 2 and "--- Part 2 ---" in got["body"]


def test_finished_evidence_counts_the_questions_answered_with_a_named_source():
    record = dict(RECORD, method="evidence", total=None)
    output = "DELIVERABLE:\nQ1. How are sign-ups counted?\nA1. By the source tag. (source: Funnel guide)\nQ2. Which videos carry it?\nA2. to be decided"
    got = mc.measure_on_completion(record, output, Registry(), scorer)
    assert got["status"] == "measured" and (got["after"], got["total"]) == (1, 2) and got["increment"]["kind"] == "evidence"


def test_a_whole_set_without_separators_is_scored_one_unit_per_heading():
    body = ("Six scripts for the videos.\n\n## Video 1: First steps\nOpening line one.\nCall to action: go.\n\n"
            "## Video 2: The plan\nOpening line two.\nNo ending here.\n\n**Video 3: Habits**\nOpening line three.\nCall to action: try it.")
    units = mc.units_by_heading(body)
    assert units[0] == "Six scripts for the videos." and len(units) == 4 and units[3].startswith("**Video 3: Habits**")
    got = mc.measure_on_completion(RECORD, "DELIVERABLE:\n" + body, Registry(), scorer)
    assert (got["before"], got["after"]) == (0, 2) and got["body"].count("--- Part") == 3
    assert mc.units_by_heading("One plain script.\nCall to action: go.") == ["One plain script.\nCall to action: go."]


def test_a_finished_deliverable_keeps_only_the_facts_its_sources_hold():
    registry = Registry()
    reference = "The brief. The product guide says a goal is tracked weekly.\n\nSOURCES:\nProduct guide: a goal is tracked weekly.\n\nDELIVERABLE:\nIt was rated best app of 2029."
    output = "DELIVERABLE:\nOpening line for tutorial 1. A goal is tracked weekly. It was rated best app of 2029. Call to action: try it today."

    def decide(prompt):
        if "The text:" in prompt:
            return scorer(prompt)
        rows = ([("plain", ""), ("fact", "a goal is tracked weekly"), ("fact", "rated best app"), ("plain", "")] if "1. Opening" in prompt else
                [("plain", ""), ("fact", "rated best app"), ("fact", "a goal is tracked weekly"), ("plain", "")])
        return json.dumps({"sentences": [{"n": n, "kind": k, "quote": q} for n, (k, q) in enumerate(rows, 1)]})
    got = mc.measure_on_completion(RECORD, output, registry, decide, reference=reference, today=datetime.date(2030, 3, 9))
    assert got["unsupported"] == ["It was rated best app of 2029."]         # the deliverable's own words after DELIVERABLE: are no source
    assert "rated best app" not in got["body"] and "A goal is tracked weekly." in got["body"]
    assert got["status"] == "measured" and got["after"] == 1                # scored on the words that are kept


GOOD = "Opening line {n}. Call to action: try it."
NO_END = "Opening line {n}. Nothing to do at the end."
NO_OPEN = "Just the ending {n}. Call to action: go."


def kept_with_unmet():
    """A kept goal as a first measure leaves it: part 1 meets the rubric, part 2 lacks its call to action."""
    first = mc.measure_on_completion(RECORD, "DELIVERABLE:\n--- Part 1 ---\n" + GOOD.format(n=1) + "\n\n--- Part 2 ---\n" + NO_END.format(n=2), Registry(), scorer)
    return first, "Kept by Quest AI: " + first["body"]


def test_the_requirement_each_part_failed_is_recorded_in_the_kept_body_and_read_back():
    first, kept = kept_with_unmet()
    assert first["after"] == 1 and first["unmet"] == [{"part": 2, "requirements": ["Ends with a call to action"]}]
    assert "Not yet meeting the rubric:\n- Part 2: Ends with a call to action" in first["body"]
    assert first["body"].index("Not yet meeting") < first["body"].index("--- Part 1 ---")          # in the overview, so it is never counted as a part
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_progress as mp
    assert len(mp.kept_parts(kept)) == 2 and NO_END.format(n=2) in mp.kept_parts(kept)[1] and "Not yet" not in "".join(mp.kept_parts(kept))
    assert mc.unmet_of(kept) == {2: ["Ends with a call to action"]} and mc.unmet_of("Overview:\nNothing failed.") == {}
    assert mc.unmet_section([]) == "" and mc.unmet_of("x" + mc.unmet_section(first["unmet"])) == {2: ["Ends with a call to action"]}


def test_a_repair_of_a_kept_part_replaces_it_only_when_the_check_finds_it_meets_more():
    first, kept = kept_with_unmet()
    registry = Registry(kept=kept)
    got = mc.measure_on_completion(RECORD, "DELIVERABLE:\n--- Part 2 ---\n" + GOOD.format(n=2), registry, scorer)
    assert got["repairs"] == [{"part": 2, "kept_new": True, "why": "it meets more of the rubric"}]
    assert (got["before"], got["after"]) == (0, 2) and got["unmet"] == [] and "Not yet meeting" not in got["body"]
    assert GOOD.format(n=2) in got["body"] and NO_END.format(n=2) not in got["body"] and GOOD.format(n=1) in got["body"] and got["body"].count("--- Part") == 2


def test_a_worse_or_equal_repair_is_discarded_and_the_failures_stay_recorded():
    first, kept = kept_with_unmet()
    worse = mc.measure_on_completion(RECORD, "DELIVERABLE:\n--- Part 1 ---\n" + NO_OPEN.format(n=1), Registry(kept=kept), scorer)
    assert [(r["part"], r["kept_new"]) for r in worse["repairs"]] == [(1, False)] and GOOD.format(n=1) in worse["body"] and NO_OPEN.format(n=1) not in worse["body"]
    assert worse["after"] == 1 and worse["unmet"] == first["unmet"]
    equal = mc.measure_on_completion(RECORD, "DELIVERABLE:\n--- Part 2 ---\nOpening line again. Still nothing at the end.", Registry(kept=kept), scorer)
    assert equal["repairs"][0]["kept_new"] is False and NO_END.format(n=2) in equal["body"]


def test_a_repair_and_a_new_part_in_one_deliverable_are_both_handled():
    first, kept = kept_with_unmet()
    output = "DELIVERABLE:\n--- Part 2 ---\n" + GOOD.format(n=2) + "\n\n--- Part 3 ---\n" + GOOD.format(n=3)
    got = mc.measure_on_completion(RECORD, output, Registry(kept=kept), scorer)
    assert got["after"] == 3 and got["body"].count("--- Part") == 3 and GOOD.format(n=3) in got["body"] and got["unmet"] == []


def test_a_repair_goes_through_the_source_check_before_it_can_replace_a_part():
    first, kept = kept_with_unmet()
    reference = "The brief.\n\nSOURCES:\nNothing relevant.\n\nDELIVERABLE:\nx"

    def decide(prompt):
        if "The text:" in prompt:
            return scorer(prompt)
        return json.dumps({"sentences": [{"n": n, "kind": "fact", "quote": "no source says this"} for n in range(1, 8)]})
    got = mc.measure_on_completion(RECORD, "DELIVERABLE:\n--- Part 2 ---\n" + GOOD.format(n=2), Registry(kept=kept), decide, reference=reference)
    assert got["unsupported"] and got["repairs"][0]["kept_new"] is False and NO_END.format(n=2) in got["body"]


def test_with_no_requirements_the_old_behaviour_holds_and_nothing_is_recorded_as_unmet():
    record = dict(RECORD, requirements=[])
    kept = "Kept by Quest AI: Overview:\nTutorials.\n\n--- Part 1 ---\nOld script one."
    got = mc.measure_on_completion(record, "DELIVERABLE:\n--- Part 1 ---\nNew script one.\n\n--- Part 2 ---\nNew script two.", Registry(kept=kept), scorer)
    assert got["after"] == 0 and got["unmet"] == [] and got["repairs"] == [] and "Not yet meeting" not in got["body"]
    assert "New script one." in got["body"] and "Old script one." not in got["body"] and got["body"].count("--- Part") == 2       # a whole body replaces, as before
