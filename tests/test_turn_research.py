"""Research a turn already did is kept on its quest after the reply: from the turn's own web reads, through the hub's recorded_research arm and the quest's store, with no
new search; and a deferred investigation (the deep path) records through the same Quest call."""
import json
import time
from datetime import date, timedelta
from types import SimpleNamespace

from quest_ai_runner.the_body.hands.quest_operations.tools_quest_investigations import investigate_for_quest_spec
from quest_ai_runner.the_world.science.research_memory import ResearchMemory
from quest_ai_runner.the_world.science.turn_research import TurnResearch, observed_from, outcome_of, wanted
from quest_ai_runner.the_world.tools.tools import ToolContext

QUEST, FUEL = "quest_a", "strategy_fuel"
SOLUTIONS = [{"id": QUEST, "name": "The whole quest"}, {"id": FUEL, "name": "Fuel"}]
TODAY = date.today().isoformat()
HITS = [{"title": "Fuelling a long run", "url": "https://example.org/fuel", "snippet": "Carbohydrates delay fatigue on long runs.", "query": "carbs long run"},
        {"title": "Race nutrition guide", "url": "https://example.com/guide", "snippet": "Most guides suggest practising fuelling in training.", "query": "carbs long run"}]
GATHERED = [{"rel_path": "web_search:carbs long run", "hits": HITS, "text": "WEB RESULTS"}, {"locator": "web extract: https://example.org/fuel", "text": "The page text in full."},
            {"locator": "query:other", "text": "not web"}]
PLAN = SimpleNamespace(research_decision="what to eat on long runs", research_criteria="independent guides or studies", research_change="a study that finds no benefit")


class Client:
    def __init__(self, earlier=()):
        self.earlier, self.recorded = list(earlier), []

    def investigation_solutions(self, quest_id):
        return SOLUTIONS

    def list_investigations(self, quest_id, **filters):
        return self.earlier

    def earlier_investigations(self, quest_id, solution_ids=(), research_question_id=""):
        return self.earlier

    def list_experiments(self, quest_id):
        return []

    def record_investigation(self, quest_id, question, **fields):
        self.recorded.append({"question": question, **fields})
        return {"investigation_id": "inv_new", "question": question, "items": fields.get("items"), "synthesis": fields.get("synthesis"), "limitations": fields.get("limitations"),
                "queries": fields.get("queries"), "knowledge": [{"item_id": "i1", "status": "proposed", "reason": "Autopilot is off for this quest"}]}


def ask(prompt):
    if "You place one question" in prompt:
        return json.dumps({"solution_ids": [FUEL], "within_scope": True})
    if "A person asked a question and an answer was written" in prompt:
        return json.dumps({"known": "Carbohydrates help on long runs.", "contested": "No disagreement seen.", "unknown": "How much per hour.", "moves": "slow"})
    return json.dumps({"answered_by": "inv_new"})


def memory(client):
    return ResearchMemory(client, QUEST, ask, TODAY)


def test_the_web_reads_of_a_turn_are_found_in_what_it_gathered():
    observed, pages = observed_from(GATHERED)
    assert observed == [{"query": "carbs long run", "hits": HITS}] and pages == {"https://example.org/fuel": "The page text in full."}


def test_only_an_answer_with_a_research_decision_and_reads_is_kept():
    answer, observed = SimpleNamespace(kind="answer"), [{"query": "q", "hits": HITS}]
    assert wanted(PLAN, answer, memory(Client()), observed)
    assert not wanted(SimpleNamespace(research_decision=""), answer, memory(Client()), observed)      # a quick fact
    assert not wanted(PLAN, answer, None, observed) and not wanted(PLAN, answer, memory(Client()), []) and not wanted(PLAN, SimpleNamespace(kind="deep"), memory(Client()), observed)


def test_the_research_is_recorded_once_from_its_own_reads_with_the_discipline_stated_before_reading():
    client = Client()
    done = TurnResearch().record(memory(client), "What should I eat on long runs?", PLAN, "Carbohydrates help on long runs.", *observed_from(GATHERED), ask=ask, today=TODAY)
    assert done["recorded"] and done["investigation_id"] == "inv_new" and len(client.recorded) == 1
    body = client.recorded[0]
    assert body["solution_ids"] == [FUEL] and body["carried_out_by"] == "quest_ai" and body["decision"] == "what to eat on long runs"
    assert [q["query"] for q in body["queries"]] == ["carbs long run"] and "independent guides or studies" in body["criteria"]
    assert body["synthesis"]["known"] == ["Carbohydrates help on long runs."] and body["synthesis"]["unknown"] == ["How much per hour."]
    assert any("a study that finds no benefit" in line for line in body["limitations"])
    assert body["refresh_on"] == (date.fromisoformat(TODAY) + timedelta(days=365)).isoformat()      # a slow subject is revisited late
    assert {i["verified"] for i in body["items"]} >= {"opened", "snippet"}                        # the page the turn opened in full, and the snippets


def test_the_same_research_asked_again_is_not_duplicated():
    first = Client()
    TurnResearch().record(memory(first), "What should I eat on long runs?", PLAN, "x", *observed_from(GATHERED), ask=ask, today=TODAY)
    body = first.recorded[0]
    held = {"investigation_id": "inv_new", "question": body["question"], "decision": body["decision"], "size": body["size"], "done_on": TODAY, "refresh_on": body["refresh_on"],
            "status": "open", "solution_ids": [FUEL], "synthesis": body["synthesis"], "limitations": body["limitations"], "items": body["items"]}
    second = Client(earlier=[held])
    done = TurnResearch().record(memory(second), "What should I eat on long runs?", PLAN, "x", *observed_from(GATHERED), ask=ask, today=TODAY)
    assert done["reused"] and second.recorded == []


def test_the_recording_runs_after_the_answer_in_one_thread_and_a_failure_is_kept_not_raised():
    holder, client = TurnResearch(), Client()
    assert holder.kickoff(memory(client), "What should I eat on long runs?", PLAN, SimpleNamespace(kind="answer", text="Carbohydrates help."), GATHERED, ask)
    for _ in range(100):
        if holder.last:
            break
        time.sleep(0.05)
    assert holder.last["recorded"] and not holder.running.is_set()
    broken = Client()
    broken.investigation_solutions = lambda quest_id: (_ for _ in ()).throw(RuntimeError("down"))
    assert TurnResearch().record(memory(broken), "q", PLAN, "x", *observed_from(GATHERED), ask=ask, today=TODAY)["recorded"] is False


def test_the_outcome_is_reported_when_the_recording_ends_and_the_reply_never_claims_it_early():
    reported = []
    holder, client = TurnResearch(), Client()
    mem = ResearchMemory(client, QUEST, ask, TODAY, report=reported.append)
    assert holder.kickoff(mem, "What should I eat on long runs?", PLAN, SimpleNamespace(kind="answer", text="Carbohydrates help."), GATHERED, ask)
    for _ in range(100):
        if reported:
            break
        time.sleep(0.05)
    assert reported[0]["outcome"] == "kept" and reported[0]["investigation_id"] == "inv_new"
    failing = Client()
    failing.investigation_solutions = lambda quest_id: (_ for _ in ()).throw(RuntimeError("down"))
    lost = outcome_of(TurnResearch().record(memory(failing), "q", PLAN, "x", *observed_from(GATHERED), ask=ask, today=TODAY))
    assert lost["outcome"] == "not_kept" and "down" in lost["reason"]
    assert outcome_of({"recorded": True, "reused": True, "investigation_id": "inv_1"})["outcome"] == "reused"
    assert outcome_of({"recorded": True, "extended": True, "investigation_id": "inv_1"})["outcome"] == "extended"


def test_a_deferred_investigation_task_records_through_the_same_quest_call_and_says_what_became_of_the_knowledge():
    """The deep path runs the investigate_for_quest tool; it ends in the same ``record_investigation`` call as a turn's own research, and passes on what Quest did with the findings."""
    hub_record = {"investigation": {"decision": "what to eat", "size": "quick", "done_on": TODAY, "refresh_on": TODAY, "kind": "review", "limitations": "snippets only",
                                    "synthesis_known": "Carbohydrates help.", "synthesis_contested": "", "synthesis_unknown": "How much."},
                  "items": [{"item_id": "i1", "claim": "Carbohydrates delay fatigue.", "source": "https://example.org/a", "independent": "yes", "verified": "snippet", "grade": "B"}],
                  "queries": [{"source": "web search", "query": "carbs", "date": TODAY, "hits": 5}], "screened_out": {}, "gaps": []}
    client = Client()
    handler = investigate_for_quest_spec({}, client_factory=lambda: client, ask=ask, carry_out=lambda question, decision, size, **kw: hub_record).handler
    result = handler({"quest_id": QUEST, "question": "Does oats help before long runs?", "decision": "what to eat", "informs": "quest_decision"}, ToolContext(quest_id=QUEST))
    assert result.ok and len(client.recorded) == 1 and client.recorded[0]["carried_out_by"] == "quest_ai"
    assert "NOT added to the strategy's knowledge (Autopilot is off for this quest)" in result.text
