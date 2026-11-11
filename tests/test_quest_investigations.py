"""Investigations for a quest: the tool that looks into a question and records it, the rules that keep a quick lookup out of the record, the reuse of a
current earlier investigation and the extension of an out of date one, and the mapping from the hub's review record to what Quest stores.
Offline: a fake Quest client, a fake fast-tier model call and a fake hub review."""
from __future__ import annotations

import json

from quest_ai_runner.the_body.hands.quest_operations.tools_quest_investigations import (
    find_earlier_investigations_spec, investigate_for_quest_spec, mark_investigation_item_spec)
from quest_ai_runner.the_world.science import investigate
from quest_ai_runner.the_world.tools.tools import STANDARD_TOOL_BUILDERS, ToolContext
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

WHOLE, FUEL = "quest_x", "sol_fuel"
SOLUTIONS = [{"id": WHOLE, "name": "Run a marathon", "parent_id": None}, {"id": FUEL, "name": "Fuel for long runs", "parent_id": WHOLE}]
HUB_RECORD = {
    "investigation": {"investigation_id": "r-1", "kind": "review", "question": "oats?", "decision": "what to eat", "size": "quick", "protocol_date": "2026-11-10",
                      "done_on": "2026-11-10", "method": "quick review", "criteria": "any date", "sources": "web search", "found": "8",
                      "synthesis_known": "Carbohydrates help", "synthesis_contested": "No disagreement", "synthesis_unknown": "Oats against other foods",
                      "limitations": "titles and result snippets only"},
    "items": [{"item_id": "i1", "claim": "Carbohydrates delay fatigue", "source": "https://example.org/a", "source_date": "2026-05-01", "independent": "yes",
               "verified": "snippet", "licence": "unknown", "grade": "B", "knowledge_id": "", "applied": "not-applied: candidate defer, grade B"}],
    "queries": [{"source": "web search", "query": "oats before running", "date": "2026-11-10", "hits": 8}],
    "screened_out": {"duplicate result": 6}, "protocol": {}, "applied": [], "gaps": []}


class FakeClient:
    def __init__(self, earlier=(), experiments=()):
        self.earlier, self.experiments, self.recorded, self.marked = list(earlier), list(experiments), [], []

    def list_experiments(self, quest_id):
        return self.experiments

    def investigation_solutions(self, quest_id):
        return SOLUTIONS

    def earlier_investigations(self, quest_id, solution_ids=(), research_question_id=""):
        self.asked_for = (list(solution_ids), research_question_id)
        return self.earlier

    def list_investigations(self, quest_id, **filters):
        return self.earlier

    def record_investigation(self, quest_id, question, **fields):
        self.recorded.append({"question": question, **fields})
        return {"investigation_id": "inv_new", "question": question, "best_grade": "B", "queries": fields.get("queries"), "items": fields.get("items"),
                "synthesis": fields.get("synthesis"), "limitations": fields.get("limitations")}

    def mark_investigation_item(self, quest_id, investigation_id, item_id, applied, **fields):
        self.marked.append((investigation_id, item_id, applied, fields))
        return {"investigation_id": investigation_id}


def answers(**by_marker):
    """A fake fast-tier call: answers by which prompt it was given."""
    def ask(prompt):
        if "You place one question" in prompt:
            return json.dumps(by_marker.get("place", {"solution_ids": [FUEL], "within_scope": True}))
        return json.dumps(by_marker.get("reuse", {"answered_by": ""}))
    return ask


def tool(client, ask, calls=None):
    def carry_out(question, decision, size, **kwargs):
        (calls if calls is not None else []).append((size, kwargs))
        return HUB_RECORD
    return investigate_for_quest_spec({}, client_factory=lambda: client, ask=ask, carry_out=carry_out).handler


def run(handler, **args):
    return handler({"quest_id": WHOLE, "question": "Does oats help before long runs?", "decision": "what to eat", "informs": "quest_decision", **args}, ToolContext(quest_id=WHOLE))


def earlier_row(reuse, investigation_id="inv_old"):
    return {"investigation_id": investigation_id, "done_on": "2026-10-01", "best_grade": "B", "question": "Does oats help?", "known": ["Carbohydrates help"],
            "unknown": [], "reuse": reuse, "on": "solution", "items": []}


def test_a_quick_factual_lookup_is_not_investigated_or_recorded():
    client, calls = FakeClient(), []
    result = run(tool(client, answers(), calls), informs="none")
    assert result.status is ToolStatus.REFUSED and not client.recorded and not calls and "web search" in result.text


def test_a_result_that_informs_a_decision_is_looked_into_under_the_narrowest_strategy_and_recorded():
    client, calls = FakeClient(), []
    result = run(tool(client, answers(), calls))
    assert result.ok and calls[0][0] == "quick"                                   # quick by default
    body = client.recorded[0]
    assert body["solution_ids"] == [FUEL] and body["carried_out_by"] == "quest_ai" and body["asked_by_kind"] == "person"
    assert body["items"][0]["grade"] == "B" and "licence" not in body["items"][0] and body["queries"][0]["hits"] == 8
    assert body["screened_out"] == [{"count": 6, "reason": "duplicate result"}]
    assert "How sure: fairly sure" in result.text and "changed nothing yet" in result.text and chr(0x2014) not in result.text


def test_with_no_narrower_strategy_it_falls_back_to_the_whole_quest():
    client = FakeClient()
    run(tool(client, answers(place={"solution_ids": ["sol_not_in_the_list"], "within_scope": True})))
    assert client.recorded[0]["solution_ids"] == [WHOLE]
    client = FakeClient()
    run(tool(client, lambda prompt: "no json at all"))
    assert client.recorded[0]["solution_ids"] == [WHOLE]


def test_a_full_investigation_needs_the_person_to_have_asked():
    client, calls = FakeClient(), []
    result = run(tool(client, answers(), calls), size="full")
    assert calls[0][0] == "standard" and "did not ask for a full one" in result.text
    calls.clear()
    run(tool(FakeClient(), answers(), calls), size="full", person_asked_for_full_investigation=True)
    assert calls[0][0] == "full"


def test_an_experiments_scope_and_held_choices_are_not_reopened():
    experiment = {"research_question_id": "rq_1", "research_question": "Which fuel works?", "in_scope": ["Pre-run food"], "held_fixed": ["Training plan"]}
    client, calls = FakeClient(experiments=[experiment]), []
    out = run(tool(client, answers(place={"solution_ids": [FUEL], "within_scope": False, "scope_note": "It would change the training plan."}), calls),
              research_question_id="rq_1")
    assert out.status is ToolStatus.REFUSED and "training plan" in out.text and not calls and not client.recorded
    unchecked = run(tool(client, lambda prompt: "?", calls), research_question_id="rq_1")      # a scope that cannot be checked counts as outside
    assert unchecked.status is ToolStatus.REFUSED and not calls
    inside = run(tool(client, answers(), calls), research_question_id="rq_1")
    assert inside.ok and client.recorded[0]["research_question_id"] == "rq_1"
    assert run(tool(client, answers(), calls), research_question_id="rq_missing").status is ToolStatus.REFUSED


def test_a_fresh_earlier_investigation_is_used_and_cited_instead_of_searching():
    client, calls = FakeClient(earlier=[earlier_row("fresh")]), []
    result = run(tool(client, answers(reuse={"answered_by": "inv_old"}), calls))
    assert result.status is not ToolStatus.NOOP and not calls and not client.recorded
    assert "inv_old" in result.text and "nothing new was searched" in result.text
    assert client.asked_for == ([FUEL], "")
    again = run(tool(client, answers(reuse={"answered_by": "inv_old"}), calls), look_again=True)
    assert again.ok and calls and client.recorded[0]["extends_investigation_id"] == "inv_old"


def test_a_stale_or_thin_earlier_investigation_is_extended_not_started_from_nothing():
    for reuse in ("stale", "partial"):
        client, calls = FakeClient(earlier=[earlier_row(reuse)]), []
        result = run(tool(client, answers(reuse={"answered_by": "inv_old"}), calls))
        assert result.ok and calls and client.recorded[0]["extends_investigation_id"] == "inv_old" and "extends the earlier investigation inv_old" in result.text
    client, calls = FakeClient(earlier=[earlier_row("fresh")]), []
    run(tool(client, answers(reuse={"answered_by": "inv_invented"}), calls))               # an id the model made up answers nothing
    assert calls and "extends_investigation_id" not in client.recorded[0]


def test_the_hub_review_is_called_with_the_strategies_as_links_and_its_own_reuse_off():
    seen = {}

    class Seat:
        def review(self, question, decision, **kwargs):
            seen.update(kwargs)
            return HUB_RECORD

    investigate.review("q", "d", "quick", research_question_id="rq_1", solution_ids=[FUEL], learning=Seat(), today="2026-11-10")
    assert seen["links"] == [f"solution:{FUEL}"] and seen["reuse"] is False and seen["use_model"] is False and seen["experiment_id"] == "rq_1"
    investigate.review("q", "d", "standard", learning=Seat())
    assert seen["use_model"] is True


def test_the_hub_record_maps_to_the_shape_quest_stores():
    body = investigate.quest_record(HUB_RECORD, solution_ids=[FUEL], asked_by="quest_ai", origin="autopilot")
    assert body["asked_by_kind"] == "quest_ai" and body["origin"] == "autopilot" and body["found"] == 8 and body["sources"] == ["web search"]
    assert body["synthesis"]["known"] == ["Carbohydrates help"] and body["limitations"] == ["titles and result snippets only"]
    assert "applied" not in body["items"][0]


def test_the_tools_are_registered_and_their_reads_do_not_write():
    for name in ("investigate_for_quest", "find_earlier_quest_investigations", "mark_quest_investigation_item"):
        assert name in STANDARD_TOOL_BUILDERS
    assert find_earlier_investigations_spec({}, client_factory=lambda: FakeClient()).mutates is False
    found = find_earlier_investigations_spec({}, client_factory=lambda: FakeClient(earlier=[earlier_row("stale")])).handler(
        {"quest_id": WHOLE, "solution_id": FUEL}, ToolContext(quest_id=WHOLE))
    assert "out of date" in found.text


def test_marking_a_finding_records_what_it_changed_or_why_not():
    client = FakeClient()
    handler = mark_investigation_item_spec({}, client_factory=lambda: client).handler
    done = handler({"quest_id": WHOLE, "investigation_id": "inv_1", "item_id": "i1", "applied": True, "what": "Added oats to the fuel note", "kind": "note"},
                   ToolContext(quest_id=WHOLE))
    assert done.ok and client.marked[0][2] is True and "Added oats" in done.text
    assert handler({"quest_id": WHOLE, "investigation_id": "inv_1", "item_id": "i1"}, ToolContext(quest_id=WHOLE)).status is ToolStatus.REFUSED


def test_a_quick_investigation_without_a_synthesis_reports_its_graded_findings():
    from quest_ai_runner.the_world.science import investigate
    text = investigate.summary_text({"question": "How long?", "best_grade": "C", "queries": [{}], "synthesis": {"known": [], "unknown": []},
                                     "items": [{"claim": "Five minutes a day is the usual start", "grade": "C", "source": "https://example.org/a"}]})
    assert "Five minutes a day is the usual start (not very sure; https://example.org/a)" in text and "nothing was settled" not in text


def test_a_search_only_web_member_still_reads_a_page_through_the_research_adapter(monkeypatch):
    from quest_ai_runner.the_world.tools.web_search import area, web_research

    class SearchOnly:
        def describe(self):
            return "search"

    class Reader:
        def fetch(self, url, **k):
            return f"page {url}"

    monkeypatch.setattr(web_research, "build_web_research_from_env", lambda env: Reader())
    seat = area.WebWithSupportCheck(SearchOnly())
    assert seat.fetch("https://example.org/a", focus="x") == "page https://example.org/a"
    assert area.WebWithSupportCheck(Reader()).fetch("https://example.org/b") == "page https://example.org/b"
