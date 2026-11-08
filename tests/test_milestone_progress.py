"""The milestone progress step on invented records: a progress cue makes one AI increment, measured before and after by a check that is not the writer, kept
on the milestone's keyed goal, with the metric read again and at most one person-only step; a repeat continues from the kept work."""
import json
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue, milestone_progress

TABLES = {"solutions": {"sol-1": {"name": "M1 Tutorials published", "milestones": "Six tutorials are published and linked on the goal",
                                  "purpose": "Teach new users the product with short tutorials.", "measures_of_success": "tutorials published"}},
          "metrics": {"met-1": {"name": "Tutorials published", "record": "Current value: 0 of 6", "solution_ids": ["sol-1"]}}}


class Registry:
    """The flow's tools over an in-memory quest: goals by title, spec records, and every call kept."""

    def __init__(self):
        self.goals, self.calls, self.tables = {}, [], json.loads(json.dumps(TABLES))

    def get(self, name):
        return name if name in ("read_spec_records", "keep_milestone_deliverable", "apply_spec_edit") else None

    def invoke(self, name, args, ctx=None):
        self.calls.append((name, args))
        if name == "read_spec_records":
            return SimpleNamespace(ok=True, data={"records": self.tables}, text="records")
        if name == "apply_spec_edit":
            change = args["change"]
            if change["kind"] == "spec_edit":
                self.tables["solutions"][change["target"]][change["field"]] = change["value"]
                return SimpleNamespace(ok=True, data={"applied": True, "target": change["target"], "field": change["field"], "after": change["value"]}, text="Done")
            self.tables["metrics"].setdefault(change["key"], dict(change["fields"]))
            return SimpleNamespace(ok=True, data={"applied": True, "created": True, "target": change["key"]}, text="Done")
        state = "updated" if args["title"] in self.goals else "created"
        self.goals[args["title"]] = args["body"]
        return SimpleNamespace(ok=True, data={"state": state, "goal": {"name": args["title"]}, "increment": args["increment"]}, text=state)


def decide(prompt):
    if "Break the work this milestone needs" in prompt:
        return json.dumps({"items": [
            {"name": "the tutorial list and the script for the next tutorial", "kind": "deliverable", "actor": "ai", "outward": False, "moves_parent": False,
             "inputs": ["sol-1"], "total": 6, "requirements": ["Has an opening", "Ends with a call to action"]},
            {"name": "recording and publishing each tutorial", "kind": "outward", "actor": "person", "outward": True, "moves_parent": True, "inputs": ["sol-1"]}]})
    if "can an AI do it right now" in prompt:
        return json.dumps({"verdicts": [{"n": 1, "ai_can": True}, {"n": 2, "ai_can": False}]})
    if "Propose ONE intermediate metric" in prompt:
        return json.dumps({"name": "Tutorial scripts meeting the rubric", "definition": "Count scripts meeting every requirement.", "unit": "scripts", "total": 6,
                           "feeds_parent": "Each tutorial is recorded from a script that meets the rubric."})
    if "You write one deliverable" in prompt:
        n = 2 if "Already kept" in prompt else 1
        return json.dumps({"overview": "Six short tutorials, one per first-week task.", "part": f"Opening line for tutorial {n}. Teach the product. Call to action: try it today."})
    if "Check one deliverable" in prompt:
        return json.dumps({"verdicts": [{"n": 1, "meets": True, "quote": "Opening line"}, {"n": 2, "meets": True, "quote": "Call to action"}]})
    if '"pick"' in prompt or "Pick" in prompt or "pick" in prompt:
        return json.dumps({"pick": "tracks", "quote": "Proposed intermediate metric"})
    return "{}"


def cue(candidates=()):
    row = TABLES["solutions"]["sol-1"]
    return milestone_cue.Resolved(rid="sol-1", name=row["name"], row=row, metric_ids=["met-1"], owners=[], week=milestone_cue.Week("2030-03-04", "2030_W10", "2030-03-10"),
                                  facts="", quest_id="q-1", done_when=row["milestones"], value="Current value: 0 of 6", purpose=row["purpose"],
                                  candidates=list(candidates), records_text=milestone_cue.records_read(TABLES, "sol-1", row), tables=TABLES)


def test_a_progress_cue_makes_a_measured_kept_increment_and_one_person_step():
    registry = Registry()
    made = milestone_progress.make_progress(cue(), registry, decide)
    assert made.status == "measured" and made.kept_state == "created"
    assert (made.before, made.after, made.total) == (0, 1, 6) and made.measure_name == "Tutorial scripts meeting the rubric"
    assert made.ask == "recording and publishing each tutorial" and made.metric_after == "Current value: 0 of 6"
    names = [n for n, _ in registry.calls]
    assert names.index("keep_milestone_deliverable") < len(names) - 1 and names[-1] == "read_spec_records"     # the metric is read again after the keep
    kept = dict(registry.calls[names.index("keep_milestone_deliverable")][1])
    assert kept["title"] == "M1 Tutorials published deliverables" and kept["increment"]["before"] == 0 and kept["increment"]["after"] == 1
    reply = made.reply_text()
    assert reply.index("Opening line for tutorial 1") < reply.index("0 of 6 before, 1 of 6 after") < reply.index("The one step only a person can do")
    assert reply.count("only a person can do") == 1


def test_a_repeat_continues_from_the_kept_work_and_updates_in_place():
    registry = Registry()
    first = milestone_progress.make_progress(cue(), registry, decide)
    kept_goal = {"title": first.title, "text": "Kept by Quest AI: " + registry.goals[first.title], "spec_solution_id": "sol-1"}
    again = milestone_progress.make_progress(cue([kept_goal]), registry, decide)
    assert again.kept_state == "updated" and (again.before, again.after) == (1, 2) and len(again.parts) == 2
    assert len(registry.goals) == 1 and again.title == first.title


def test_the_deliverable_lands_on_the_goal_quest_ai_already_keeps_for_the_milestone():
    registry = Registry()
    kept = {"title": "M1 Tutorials published next step: week of 2030-03-04", "text": "Kept by Quest AI: Next step.", "spec_solution_id": "sol-1"}
    persons = {"title": "M1 Tutorials published plan", "text": "A person's own notes.", "spec_solution_id": "sol-1"}
    made = milestone_progress.make_progress(cue([persons, kept]), registry, decide)
    assert made.title == kept["title"] and list(registry.goals) == [kept["title"]]          # a person's goal is never the target


def test_a_writer_claim_without_a_quote_in_the_text_does_not_count():
    scorer = milestone_progress.Scorer(lambda prompt: json.dumps({"verdicts": [{"n": 1, "meets": True, "quote": "words not in the text"}]}), ["Has an opening"])
    assert scorer("Has an opening", "Some other text here.") is False


def test_a_dated_reading_line_is_read_again_as_its_value_and_day():
    registry = Registry()
    registry.tables["metrics"]["met-1"]["record"] = "Current value: 0 (as of 2030-03-09, from the funnel dashboard, sign-ups by source)"
    made = milestone_progress.make_progress(cue(), registry, decide)
    assert made.metric_after == "0 (as of 2030-03-09, from the funnel dashboard, sign-ups by source)"


def test_the_rubric_is_kept_on_the_milestones_record_once_and_the_next_turn_reuses_it():
    registry = Registry()
    first = milestone_progress.make_progress(cue(), registry, decide)
    knowledge = registry.tables["solutions"]["sol-1"]["knowledge"]
    assert first.rubric_state == "kept" and knowledge.startswith("[best guess] Deliverable rubric for the tutorial list and the script for the next tutorial (6 units): ")
    assert "Has an opening; Ends with a call to action (source: sol-1, Quest AI, " in knowledge
    asked = []

    def drifting(prompt):
        asked.append(prompt)
        if "Break the work this milestone needs" in prompt:       # a later decomposition proposes other requirements: the kept rubric wins
            return decide(prompt).replace("Has an opening", "Names a title").replace("Ends with a call to action", "Lists shots")
        return decide(prompt)
    later = cue()
    later.row = dict(later.row, knowledge=knowledge)
    second = milestone_progress.make_progress(later, registry, drifting)
    assert second.rubric_state == "reused" and second.item == "the tutorial list and the script for the next tutorial"
    writer = next(p for p in asked if "You write one deliverable" in p)
    assert "Has an opening; Ends with a call to action" in writer and "Names a title" not in writer
    assert all("Names a title" not in p for p in asked if "Check one deliverable" in p)
    assert registry.tables["solutions"]["sol-1"]["knowledge"].count("Deliverable rubric") == 1


def test_a_research_increment_counts_only_answers_with_a_quote_found_in_the_records():
    def research(prompt):
        if "Break the work this milestone needs" in prompt:
            return json.dumps({"items": [{"name": "the questions the tutorial choice needs", "kind": "evidence", "actor": "ai", "outward": False, "moves_parent": False,
                                          "requirements": ["Each answer cites a record"]},
                                         {"name": "publishing", "kind": "outward", "actor": "person", "outward": True, "moves_parent": True, "step": "Publish them."}]})
        if "You gather the evidence" in prompt:
            return json.dumps({"questions": [
                {"question": "What is the milestone for?", "answer": "Teaching new users the product.", "source": "the milestone record", "quote": "Teach new users the product"},
                {"question": "How many users watched?", "answer": "About 400.", "source": "analytics", "quote": "400 viewers last week"},
                {"question": "Which tutorial goes first?", "answer": "", "source": "", "quote": ""}]})
        return decide(prompt)
    made = milestone_progress.make_progress(cue(), Registry(), research)
    assert made.status == "measured" and (made.before, made.after, made.total) == (0, 1, 3)
    text = made.reply_text()
    assert "A1. Teaching new users the product. (source: the milestone record)" in text and "A2. to be decided" in text and "400" not in text
    again = milestone_progress.questions_of(made.parts[0])
    assert [bool(q["answer"]) for q in again["questions"]] == [True, False, False]       # a kept evidence part is measured again the same way


def test_the_milestone_pass_shapes_a_made_progress_without_running_the_work_again():
    made = milestone_progress.Progress(item="the script", measure_name="Scripts meeting the rubric", before=0, after=1, total=6, as_of="2030-03-10",
                                       status="measured", ask="publish it")
    found = milestone_progress.milestone_pass(made, cue(), "get it moving")
    assert found["solution_id"] == "sol-1" and found["target"] == {"metric_id": "met-1", "value": 6, "done_when": TABLES["solutions"]["sol-1"]["milestones"]}
    assert found["needs"].startswith("the script: Scripts meeting the rubric: 0 of 6 before, 1 of 6 after") and found["risks"] == ["Only a person can do: publish it"]


def test_a_unit_short_of_its_rubric_is_built_out_by_the_hub_depth_loop_and_rechecked():
    def thin_first(prompt):
        if "You write one deliverable" in prompt and "## This part" not in prompt:
            return json.dumps({"part": "Opening line for tutorial 1. Teach the product."})          # misses the call to action
        if "## This part" in prompt:
            return json.dumps({"part": "Call to action: try it today."})
        if "Join these parts" in prompt:
            return json.dumps({"part": "Opening line for tutorial 1. Teach the product. Call to action: try it today."})
        if "Check one deliverable" in prompt:
            text = prompt.split("The text:", 1)[1]
            return json.dumps({"verdicts": [{"n": n, "meets": True, "quote": q} for n, q in enumerate(("Opening line", "Call to action"), 1) if q in text]
                              if "Has an opening" in prompt.split("Requirements:")[1].split("\n")[1] or True else []})
        return decide(prompt)
    made = milestone_progress.make_progress(cue(), Registry(), thin_first)
    assert made.depth["levels"] == 1 and made.depth["before"] < 1 and made.depth["after"] == 1 and made.depth["met"]
    assert made.after == 1 and "Call to action" in made.parts[-1]
    assert "after building it out in 1 level(s) of parts" in made.reply_text()
