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
            if change["kind"] in ("spec_edit", "metric_edit"):
                table = "solutions" if change["kind"] == "spec_edit" else "metrics"
                self.tables[table][change["target"]][change["field"]] = change["value"]
                return SimpleNamespace(ok=True, data={"applied": True, "target": change["target"], "field": change["field"], "after": change["value"]}, text="Done")
            self.tables.setdefault(change.get("table") or "metrics", {}).setdefault(change["key"], dict(change["fields"]))
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
    assert reply.index("Opening line for tutorial 1") < reply.index("0 before, 1 after of 6 planned (counted: units in which a separate check found every rubric item") < reply.index("The one step only a person can do")
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
    assert first.rubric_state == "kept" and knowledge.startswith("[best guess] Deliverable rubric for the tutorial list and the script for the next tutorial (6 units) (done test ")
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
    assert found["needs"].startswith("the script: Scripts meeting the rubric: 0 before, 1 after of 6 planned") and found["risks"] == ["Only a person can do: publish it"]


def test_a_unit_short_of_its_rubric_is_built_out_by_the_hub_depth_loop_and_rechecked():
    def thin_first(prompt):
        if "You write one deliverable" in prompt and "## This part" not in prompt:
            return json.dumps({"part": "Opening line for tutorial 1. Teach the product."})          # misses the call to action
        if "still lacks" in prompt:
            return json.dumps({"part": "Call to action: try it today."})
        if "as a single coherent text" in prompt:            # the combine pass writes the whole unit from the unit and its pieces
            return json.dumps({"part": "Opening line for tutorial 1. Teach the product. Call to action: try it today."})
        if "Check one deliverable" in prompt:
            text = prompt.split("The text:", 1)[1]
            return json.dumps({"verdicts": [{"n": n, "meets": True, "quote": q} for n, q in enumerate(("Opening line", "Call to action"), 1) if q in text]
                              if "Has an opening" in prompt.split("Requirements:")[1].split("\n")[1] or True else []})
        return decide(prompt)
    made = milestone_progress.make_progress(cue(), Registry(), thin_first)
    assert made.depth["levels"] >= 1 and made.depth["before"] < 1 and made.depth["after"] == 1 and made.depth["met"]
    assert made.after == 1 and "Call to action" in made.parts[-1]
    assert "%" not in made.reply_text()          # the depth scores stay in the kept record, never as an unread figure in the reply


def test_the_writer_gets_research_and_proposes_units_on_its_own_tier():
    written, researched = [], []

    def writer(prompt):
        written.append(prompt)
        return json.dumps({"units": [{"n": 1, "topic": "Setting a first goal", "why": "the guide says users start with one goal", "source": "product guide"}],
                           "part": "Opening line for tutorial 1. Set one goal. Call to action: try it today."})
    made = milestone_progress.make_progress(cue(), Registry(), decide, write=writer, research=lambda q: researched.append(q) or "product guide: users start with one goal.",
                                            tier="balanced")
    assert researched and any("product guide: users start with one goal." in p for p in written if "You write one deliverable" in p)
    assert "Proposed units (an AI proposal from the sources, to be agreed):\n1. Setting a first goal. Why: the guide says users start with one goal (source: product guide)" in made.overview
    assert made.sources == "product guide: users start with one goal."


def test_a_metric_behind_its_source_is_read_as_the_increment_before_any_deliverable():
    registry = Registry()
    registry.tables["metrics"]["met-1"].update(record="Current value: 2 (as of 2030-03-01, from the sheet)", source="sheet")
    later = cue()
    later.tables = registry.tables
    later.source, later.metric_ids = "sheet", ["met-1"]
    later.read_source = lambda reg: setattr(later, "reading", {"value": 5, "as_of": "2030-03-09", "source": "the sheet"})
    made = milestone_progress.make_progress(later, registry, decide)
    assert made.status == "measured" and (made.before, made.after) == (2, 5) and made.parts == ["Current value: 5 (as of 2030-03-09, from the sheet)"]
    kept = next(args for name, args in registry.calls if name == "keep_milestone_deliverable")
    assert kept["increment"]["kind"] == "metric_read" and kept["increment"]["method"] == "read"


def test_the_item_the_done_test_is_made_from_comes_first():
    def answer(prompt):
        if "Break the work this milestone needs" in prompt:
            return json.dumps({"items": [
                {"name": "a side note", "kind": "deliverable", "actor": "ai", "requirements": ["Is short"]},
                {"name": "the scripts", "kind": "deliverable", "actor": "ai", "done_material": True, "requirements": ["Has an opening"]}]})
        if "can an AI do it right now" in prompt:
            return json.dumps({"verdicts": [{"n": 1, "ai_can": True}, {"n": 2, "ai_can": True}]})
        return "{}"
    items = milestone_progress.work_items("records", ["sol-1"], answer)
    assert [i["name"] for i in items] == ["the scripts", "a side note"] and items[0]["checks"] == [True, True]


def test_a_fact_sentence_no_source_holds_is_taken_out_and_proposals_stay():
    reference = "The product guide says a goal is tracked weekly."
    text = "A goal is tracked weekly. It was rated best app of 2029. We propose a first video about tracking. Try it today."

    def views(prompt):
        return json.dumps({"sentences": [{"n": n, "kind": k, "quote": q} for n, (k, q) in enumerate(
            [("fact", "a goal is tracked weekly"), ("fact", "rated best app"), ("proposal", ""), ("plain", "")] if "1. A goal" in prompt else
            [("plain", ""), ("proposal", ""), ("fact", "rated best app"), ("fact", "a goal is tracked weekly")], 1)]})
    kept, removed = milestone_progress.supported_sentences(text, reference, views)
    assert removed == ["It was rated best app of 2029."]
    assert kept == "A goal is tracked weekly. We propose a first video about tracking. Try it today."


def test_the_rubric_is_researched_agreed_by_a_second_view_and_kept_with_its_sources():
    def answer(prompt):
        if "Define what a good unit of this deliverable must meet" in prompt:
            return json.dumps({"requirements": [{"text": "Opens with a hook", "source": "style guide"}, {"text": "Ends with a call to action", "source": "style guide"},
                                                {"text": "Names the sponsor", "source": "nothing"}]})
        if "These requirements were proposed" in prompt:
            return json.dumps({"verdicts": [{"n": 1, "keep": True}, {"n": 2, "keep": True}, {"n": 3, "keep": False}]})
        return decide(prompt)
    registry = Registry()
    milestone_progress.make_progress(cue(), registry, answer, research=lambda q: "style guide: a good script opens with a hook and ends with a call to action.")
    knowledge = registry.tables["solutions"]["sol-1"]["knowledge"]
    assert ": Opens with a hook; Ends with a call to action (source: style guide, sol-1, Quest AI, " in knowledge and "sponsor" not in knowledge


def test_the_deliverable_to_make_is_confirmed_by_independent_views():
    items = [{"name": "a side note", "kind": "deliverable", "actor": "ai", "checks": [True, True], "requirements": ["x"]},
             {"name": "the scripts", "kind": "deliverable", "actor": "ai", "checks": [True, True], "requirements": ["y"]}]
    picked = milestone_progress.confirmed_item_order(items, lambda i: True, "records", lambda prompt: json.dumps({"pick": "i1", "quote": "the scripts"}))
    assert [i["name"] for i in picked] == ["the scripts", "a side note"]
    assert milestone_progress.confirmed_item_order(items[:1], lambda i: True, "records", lambda prompt: "{}") == items[:1]


def test_a_dated_reading_is_said_as_its_value_and_ids_never_reach_the_reader():
    assert milestone_cue.reading_value("Current value: 0 (as of 2030-03-08, from the funnel dashboard)\nmore") == "0 (as of 2030-03-08)"
    assert milestone_cue.reading_value("0 of 6. Read on Monday.") == "0 of 6"
    assert milestone_progress.without_ids("Ann (rep_4e8c0da18324) publishes each video.") == "Ann publishes each video."


def test_a_proposed_items_name_stays_while_its_unsupported_reasons_go():
    text = "1. Video about goals. The guide says goals are tracked.\n2. Video about teams. Trusted by thousands of teams."
    marks = {"Video about goals.": ("fact", ""), "The guide says goals are tracked.": ("fact", "goals are tracked weekly"),
             "Video about teams.": ("fact", ""), "Trusted by thousands of teams.": ("fact", "")}

    def views(prompt):
        lines = [l for l in prompt.split("The sentences:")[1].splitlines() if l[:1].isdigit() and ". " in l]
        return json.dumps({"sentences": [{"n": n, "kind": marks[l.split(". ", 1)[1]][0], "quote": marks[l.split(". ", 1)[1]][1]} for n, l in enumerate(lines, 1)
                                         if l.split(". ", 1)[1] in marks]})
    kept, removed = milestone_progress.supported_sentences(text, "Goals are tracked weekly.", views, keep_line_starts=True)
    assert removed == ["Trusted by thousands of teams."] and "Video about teams." in kept and "Video about goals." in kept


def test_a_unit_that_depends_on_the_proposed_list_is_done_in_the_same_turn_and_measured():
    asked = []

    def answer(prompt):
        asked.append(prompt)
        if "Break the work this milestone needs" in prompt:
            return json.dumps({"items": [
                {"name": "the tutorial list", "kind": "deliverable", "actor": "ai", "done_material": True, "total": 1, "requirements": ["Has an opening"]},
                {"name": "the script for one tutorial", "kind": "deliverable", "actor": "ai", "depends_on": "the tutorial list", "total": 6,
                 "requirements": ["Ends with a call to action"]},
                {"name": "publishing", "kind": "outward", "actor": "person", "outward": True, "moves_parent": True, "step": "Publish them."}]})
        if "can an AI do it right now" in prompt:
            return json.dumps({"verdicts": [{"n": 1, "ai_can": True}, {"n": 2, "ai_can": True}, {"n": 3, "ai_can": False}]})
        if "Which of these work items" in prompt:
            return json.dumps({"pick": "i0", "quote": "the tutorial list"})
        if "You write one deliverable" in prompt and "the script for one tutorial" in prompt:
            return json.dumps({"part": "Call to action: set your first goal today."})
        if "You write one deliverable" in prompt:
            return json.dumps({"units": [{"n": 1, "topic": "Setting a first goal", "why": "users start there", "source": "guide"}], "part": "Opening line for the list."})
        if "Check one deliverable" in prompt:
            text, wanted = prompt.split("The text:", 1)[1].split("Requirements:", 1)
            reqs = [l.split(". ", 1)[1] for l in wanted.splitlines() if l[:1].isdigit()]
            quote = {"Has an opening": "Opening line", "Ends with a call to action": "Call to action"}
            return json.dumps({"verdicts": [{"n": n, "meets": quote[r] in text, "quote": quote[r]} for n, r in enumerate(reqs, 1)]})
        return decide(prompt)
    registry = Registry()
    made = milestone_progress.make_progress(cue(), registry, answer)
    assert made.follow_on["item"] == "the script for one tutorial" and (made.follow_on["before"], made.follow_on["after"], made.follow_on["total"]) == (0.0, 1.0, 6)
    assert any("Write the unit for the first proposed item: Setting a first goal." in p for p in asked)
    kept = registry.goals[made.title]
    assert "--- Then, depending on it: the script for one tutorial ---\nCall to action: set your first goal today." in kept
    assert milestone_progress.kept_follow_on("Kept by Quest AI: " + kept) == ("the script for one tutorial", "Call to action: set your first goal today.")
    assert "Then, depending on it, the script for one tutorial: 0 before, 1 after of 6 planned" in made.reply_text()


def test_a_number_or_status_word_the_sources_do_not_hold_is_rewritten_or_taken_out():
    reference = "The milestone: 13 sign-ups, 10 with no source. Done when the niche is confirmed from funnel data."
    assert milestone_progress.marked_claims("Of 13 sign-ups, 10 had no source.", reference) == []
    assert milestone_progress.marked_claims("A niche counts at 30 sign-ups and 20 percent conversion.", reference) == ["30", "20"]
    assert milestone_progress.marked_claims("The list of 6 videos is agreed.", reference) == ["agreed"]
    assert milestone_progress.marked_claims("It was cross-checked against the admin user list.", reference) == ["cross-checked"]
    assert milestone_progress.marked_claims("The niche is confirmed from funnel data.", reference) == []          # held by a source sentence about the same thing
    rewrite = lambda prompt: json.dumps({"sentences": [{"n": 1, "text": "The threshold is to be set from the funnel dashboard."}]})   # noqa: E731
    assert milestone_progress.checked_line("A niche counts at 30 sign-ups.", reference, rewrite) == "The threshold is to be set from the funnel dashboard."
    assert milestone_progress.checked_line("Scripts for the 6 agreed videos", reference, lambda p: "{}") == "Scripts for the 6 videos"


def test_the_work_item_is_pinned_by_what_the_done_test_is_made_of():
    items = [{"name": "a test plan", "kind": "deliverable", "actor": "ai", "checks": [True, True], "inputs": [], "requirements": ["r"]},
             {"name": "a weekly note", "kind": "deliverable", "actor": "ai", "checks": [True, True], "inputs": [], "requirements": ["r"]},
             {"name": "decide the niche", "kind": "outward", "actor": "person", "checks": [False, False], "inputs": [], "tools": ["outward_act"]}]
    doable = lambda i: i["actor"] == "ai"      # noqa: E731
    c = cue()
    decision = milestone_progress.pinned_items(items, milestone_progress.DECISION, doable, c, "Niche confirmed", "", decide)
    assert decision[0]["name"].startswith("Comparison of the options") and decision[0]["requirements"][0] == milestone_progress.DECISION_REQUIREMENT
    assert [i["name"] for i in decision[1:]] == ["decide the niche"]          # no other AI deliverable is left for the ladder to pick
    others = milestone_progress.pinned_items(items, milestone_progress.OTHERS, doable, c, "Users from a source", "", decide)
    assert others[0]["kind"] == "evidence" and [i["name"] for i in others[1:]] == ["decide the niche"]
    assert milestone_progress.done_test_class(c, "No (as of 2030-03-01)", lambda p: "{}") == milestone_progress.DECISION     # a yes/no metric is a decision by code


def test_a_kept_rubric_stays_the_item_until_the_done_test_changes():
    registry = Registry()
    first = milestone_progress.make_progress(cue(), registry, decide)
    knowledge = registry.tables["solutions"]["sol-1"]["knowledge"]
    changed = cue()
    changed.row = dict(changed.row, knowledge=knowledge)
    changed.done_when = "Ten tutorials are published"
    again = milestone_progress.make_progress(changed, registry, decide)
    assert first.rubric_state == "kept" and again.rubric_state == "kept"         # a new done test frees the pinned item: it is defined again


def test_depth_checks_the_unit_as_it_will_be_kept():
    asked = []

    def stripping(prompt):
        asked.append(prompt)
        if "Classify each numbered sentence" in prompt:      # the claim check finds the opening an unsupported fact, so the kept text lacks it
            return json.dumps({"sentences": [{"n": n, "kind": "fact" if "Opening" in line else "plain", "quote": ""}
                                             for n, line in enumerate(prompt.split("The sentences:")[1].strip().splitlines()[:-1], 1) if line[:1].isdigit()]})
        return decide(prompt)
    made = milestone_progress.make_progress(cue(), Registry(), stripping)
    assert made.depth is None or made.depth.get("before", 0) < 1.0             # the loop never sees the raw text as met
    assert all("Opening line" not in p for p in made.parts)


def test_the_reply_names_every_artifact_kept_on_the_record():
    registry = Registry()
    made = milestone_progress.make_progress(cue(), registry, decide)
    reply = made.reply_text()
    assert "Also kept on the milestone's record this turn:" in reply and "The rubric each unit of the tutorial list" in reply
    lines = milestone_progress.kept_artifacts([{"edit": {"field": "knowledge", "value": "[best guess] Source proposal for metric met-1: read the funnel export; "
                                                                                     "a person connects the export (source: met-1 record, 2030-03-09)"}, "state": "kept"}],
                                             "", {"met-1": {"name": "Users from a source"}})
    assert lines == ["A proposal (the AI's, to be agreed) for making Users from a source readable here: read the funnel export; a person connects the export."]


def test_no_rubric_line_reaches_a_kept_artifact():
    def echoing(prompt):
        if "You write one deliverable" in prompt:       # the writer repeats its rubric and talks about the unit, around the real content
            return json.dumps({"part": "Opening line for tutorial 1. This script has an opening and ends with a call to action. Has an opening. "
                                       "Teach the product. Call to action: try it today."})
        return decide(prompt)
    registry = Registry()
    made = milestone_progress.make_progress(cue(), registry, echoing)
    body = registry.goals[made.title]
    rubric = [r for r in ("Has an opening", "Ends with a call to action")]
    for unit in made.parts:
        assert not any(r.lower() in unit.lower() for r in rubric) and "this script" not in unit.lower()
    assert "Opening line for tutorial 1" in body and "Has an opening." not in body.split("--- Part 1 ---")[1]
    text, removed = milestone_progress.without_rubric_echo("A clear hook. The unit names the single topic and ends with a call to action.",
                                                          ["Names the single topic explicitly and ends with a call to action"])
    assert text == "A clear hook." and removed


def test_an_answer_the_check_emptied_is_open_again_never_a_bare_source():
    found = milestone_progress.questions_of("Q1. How many?\nA1. (source: the record)\nQ2. Who?\nA2. The owner. (source: the record)")
    assert found["questions"][0]["answer"] == "" and found["questions"][1]["answer"] == "The owner."


def test_the_person_step_points_only_at_what_was_kept():
    rewrite = lambda prompt: json.dumps({"step": "A person adds the tagged link from the kept list to each video."})      # noqa: E731
    step = milestone_progress.kept_step("A person pastes the proposed copy into each video.", "Tagged link list: one link per video.", "", rewrite)
    assert step == "A person adds the tagged link from the kept list to each video."
    assert milestone_progress.kept_step("A person records the video.", "Script.", "", lambda p: "{}") == "A person records the video."


def test_a_link_or_product_claim_needs_a_source():
    assert milestone_progress.marked_claims("Start today at example.org.", "The records name no site.") == ["example.org"]
    assert milestone_progress.marked_claims("Start today at example.org.", "Sign up at example.org/start.") == []


def test_a_source_label_stands_only_for_a_source_the_turn_read():
    reads = "Metric record: Sign-ups by source. Recorded: 13 sign-ups. Product guide: getting started with your first goal."
    assert milestone_progress.held_source("Metric record: Sign-ups by source", reads) == "Metric record: Sign-ups by source"
    assert milestone_progress.held_source("Product card 'Proposed niche candidates'", reads) == ""


def test_the_whole_metric_records_are_read_and_the_records_text_rebuilt():
    long_record = "Current value: 0 of 6. " + "x" * 300 + " The tail fact: 42 links."

    class Whole(Registry):
        def invoke(self, name, args, ctx=None):
            if name == "read_spec_records" and "met-1" in (args.get("whole_record_ids") or []):
                tables = json.loads(json.dumps(self.tables))
                tables["metrics"]["met-1"]["record"] = long_record
                return SimpleNamespace(ok=True, data={"records": tables}, text="records")
            return super().invoke(name, args, ctx)
    c = cue()
    milestone_progress.whole_row(Whole(), c)
    assert "The tail fact: 42 links." in c.records_text


def test_an_open_question_is_its_own_researched_piece():
    def research(query):
        return "Funnel guide: sign-ups from video links are counted by the tag utm_source." if "counted" in query else "Guide: nothing on that."

    def answering(prompt):
        if "List the questions this decision needs" in prompt:
            return json.dumps({"questions": [{"question": "How are sign-ups from video links counted?", "answer": "", "source": "", "quote": ""}]})
        if "Answer one question a team's decision needs" in prompt:
            return json.dumps({"answer": "By the tag utm_source.", "source": "Funnel guide", "quote": "counted by the tag utm_source"})
        return decide(prompt)
    items = [{"name": "Evidence on sign-ups", "kind": "evidence", "actor": "ai", "outward": False, "moves_parent": False, "inputs": ["sol-1"], "total": 1,
              "requirements": ["Each answer names its source"]}]

    def decomposing(prompt):
        if "Break the work this milestone needs" in prompt:
            return json.dumps({"items": items})
        if "can an AI do it right now" in prompt:
            return json.dumps({"verdicts": [{"n": 1, "ai_can": True}]})
        if "What is this milestone's done test made of" in prompt:
            return json.dumps({"pick": "others"})
        return answering(prompt)
    made = milestone_progress.make_progress(cue(), Registry(), decomposing, research=research)
    assert made.depth and made.depth["before"] == 0 and made.depth["after"] == 1 and made.depth["stopped"] == "met"
    assert "By the tag utm_source. (source: Funnel guide)" in made.parts[0]


def test_the_deliverable_is_filed_under_its_own_solution_created_once_and_reused():
    def filing(prompt):
        if "Name the KIND of deliverable it is" in prompt:
            return json.dumps({"kind": "tutorial scripts", "purpose": "Short spoken scripts that teach one task each."})
        return decide(prompt)
    registry = Registry()
    first = milestone_progress.make_progress(cue(), registry, filing)
    home = registry.tables["solutions"].get("Tutorial scripts")
    assert home is not None and home["knowledge"].startswith("[best guess] Deliverable rubric for the tutorial list and the script for the next tutorial")
    assert "knowledge" not in registry.tables["solutions"]["sol-1"]            # the rubric belongs to the deliverable's solution, not the milestone
    assert "A new solution record: Tutorial scripts." in first.reply_text()

    asked = []

    def again(prompt):
        asked.append(prompt)
        if "Which of these solutions is the one whose own work makes this deliverable" in prompt:
            return json.dumps({"pick": "Tutorial scripts", "quote": "Tutorial scripts"})
        return filing(prompt)
    later = cue()
    later.tables = json.loads(json.dumps(registry.tables))
    found = milestone_progress.deliverable_home(later, {"name": "tutorial scripts for the next tutorial"}, None, again)
    assert found and found["id"] == "Tutorial scripts" and found["created"] is False
