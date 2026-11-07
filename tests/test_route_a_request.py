"""Quest's arm of Route a Request, offline: the planner decision is read by structure, and the private labelled cases carry labels and ids but are never written out."""
import json
from types import SimpleNamespace

import pytest

from hubofhubs.api import route_from_report
from quest_ai_runner.the_intelligence.mind.request_router.planner_derived import report_of


def plan(**fields):
    return SimpleNamespace(**{"action": "answer", "user_intent": None, "deep_target": None, **fields})


@pytest.mark.parametrize("decision,route", [
    (plan(action="answer", user_intent="ask"), "information"),
    (plan(action="read", user_intent="ask"), "information"),
    (plan(action="answer", user_intent="inform"), "information"),
    (plan(action="deep", user_intent="act"), "act"),
    (plan(action="tool"), "act"),
    (plan(action="confirm"), "act"),
    (plan(action="answer", user_intent="act", deep_target="draft_in_chat"), "draft"),
    (plan(action="clarify", user_intent="act"), "clarify"),
])
def test_the_route_is_read_off_the_planner_decision_by_structure(decision, route):
    assert route_from_report(report_of(decision))["route"] == route


def test_the_private_routes_are_found_through_the_environment_and_carry_no_text_into_a_label(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation" / "components"))
    from route_a_request import private_routes
    (tmp_path / "poorly_answered_inputs.jsonl").write_text(json.dumps({"case_id": "X-1", "input_text": "invented request one"}) + "\n" + json.dumps({"case_id": "X-2", "input_text": "invented two"}) + "\n")
    (tmp_path / "route_labels.jsonl").write_text(json.dumps({"source": "poorly_answered_inputs", "case_id": "X-1", "route": "draft", "basis": "test"}) + "\n"
                                                 + json.dumps({"source": "poorly_answered_inputs", "case_id": "X-9", "route": "act", "basis": "no text for it"}) + "\n"
                                                 + json.dumps({"source": "poorly_answered_inputs", "case_id": "X-2", "route": "deep", "basis": "not a route"}) + "\n")
    monkeypatch.setattr(private_routes, "SOURCES_FILE", tmp_path / "local.json")
    env = {"QAR_PRIVATE_POOR_ANSWERS_DATASET": str(tmp_path)}
    cases = private_routes.load_cases(env)
    assert [(c["id"], c["expect"], c["args"]["request"]) for c in cases] == [("private-X-1", "draft", "invented request one")]
    assert "invented" not in (tmp_path / "local.json").read_text() and private_routes.digest_part(env).startswith("route_labels:")
    assert private_routes.load_cases({}) == [] and private_routes.digest_part({}) == private_routes.ABSENT and not (tmp_path / "local.json").exists()
