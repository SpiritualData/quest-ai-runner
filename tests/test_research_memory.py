"""Research memory: the planner's web read first asks the quest's earlier research (the hub's recorded_research arm judges it); a quest with none costs nothing."""
import json

from quest_ai_runner.the_body.internal_context.adapters_values import Observation
from quest_ai_runner.the_world.science.research_memory import RecordedWebResearch, ResearchMemory, hub_row

QUEST = "quest_a"
SOLUTIONS = [{"id": QUEST, "name": "The whole quest"}, {"id": "strategy_1", "name": "Sleep"}]


def row(**over):
    base = {"investigation_id": "inv_1", "question": "Which sleep routines have evidence behind them?", "decision": "pick a routine", "size": "quick", "done_on": "2026-11-10",
            "refresh_on": "2099-01-01", "status": "open", "solution_ids": ["strategy_1"], "synthesis": {"known": ["A fixed wake time helps."], "unknown": ["Effect size."]},
            "limitations": ["Snippets only."], "items": []}
    return {**base, **over}


class Client:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def list_investigations(self, quest_id, **filters):
        self.calls.append("list")
        return self.rows

    def investigation_solutions(self, quest_id):
        return SOLUTIONS

    def earlier_investigations(self, quest_id, solution_ids=(), research_question_id=""):
        self.calls.append("earlier")
        return self.rows


class Web:
    def __init__(self):
        self.searches = []

    def search(self, queries, fresh=False, **kwargs):
        self.searches.append(queries)
        return Observation(kind="query", locator="web", text="fresh results", hits=[])

    def describe(self):
        return "fake web"


def asking(answered_by):
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return json.dumps({"solution_ids": ["strategy_1"], "within_scope": True, "answered_by": answered_by})
    ask.prompts = prompts
    return ask


def wrapped(rows, answered_by="inv_1"):
    client, web = Client(rows), Web()
    return RecordedWebResearch(web, ResearchMemory(client, QUEST, asking(answered_by), "2026-11-11")), web, client


def test_a_quest_with_no_research_searches_at_once_and_asks_no_model():
    memory, web, client = wrapped([])
    ask = memory.memory.ask
    assert memory.search(["sleep routines evidence"]).text == "fresh results" and web.searches and client.calls == ["list"] and ask.prompts == []


def test_a_fresh_earlier_investigation_that_answers_is_the_read_and_nothing_is_searched():
    memory, web, _ = wrapped([row()])
    found = memory.search(["Which sleep routines have evidence behind them?"])
    assert web.searches == [] and "done 2026-11-10" in found.text and "A fixed wake time helps." in found.text and "Effect size." in found.text
    assert "earlier research from 2026-11-10" in found.text and found.locator == "earlier research"


def test_fresh_true_searches_again():
    memory, web, _ = wrapped([row()])
    assert memory.search(["sleep routines"], fresh=True).text == "fresh results" and web.searches


def test_an_unrelated_query_the_judge_declines_is_searched():
    memory, web, _ = wrapped([row()], answered_by="")
    assert memory.search(["a password manager with an audit"]).text == "fresh results" and web.searches


def test_a_stale_earlier_investigation_goes_first_and_the_search_still_runs():
    memory, web, _ = wrapped([row(refresh_on="2026-10-01")])
    found = memory.search(["Which sleep routines have evidence behind them?"])
    assert web.searches and found.text.startswith("Earlier research on this quest") and "out of date" in found.text and found.text.endswith("fresh results")


def test_other_adapter_members_pass_through_and_a_failed_read_means_no_memory():
    memory, web, client = wrapped([row()])
    assert memory.describe() == "fake web"
    client.list_investigations = lambda quest_id, **kw: (_ for _ in ()).throw(RuntimeError("down"))
    assert memory.search(["anything"]).text == "fresh results"


def test_the_tool_says_what_became_of_the_findings_as_knowledge():
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_investigations import knowledge_note
    assert knowledge_note({}) == ""
    assert "1 finding(s) were added" in knowledge_note({"knowledge": [{"status": "applied"}, {"status": "unchanged"}]})
    held = knowledge_note({"knowledge": [{"status": "proposed", "reason": "Autopilot is off for this quest"}]})
    assert "NOT added" in held and "Autopilot is off" in held and "still recorded" in held


def test_the_hub_cells_of_a_quest_investigation():
    cells = hub_row(row())
    assert cells["links"] == "solution:strategy_1" and cells["synthesis_known"] == "A fixed wake time helps." and cells["refresh_on"] == "2099-01-01"
