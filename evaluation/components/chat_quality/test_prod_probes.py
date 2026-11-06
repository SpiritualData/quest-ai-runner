"""prod_probes: the metric before and after, the messages in order and the two-message standard, on invented data and no model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_probes as P  # noqa: E402


class Client:
    def __init__(self, answers):
        self.answers = answers

    def _request(self, method, path):
        assert method == "GET"
        return self.answers[path]


PROBE = {"name": "paid users", "read": "/goals", "list_key": "goals", "match": {"title": "Reach"}, "regex": r"Current value:\s*([\d,.]+)",
         "computed": {"read": "/funnel", "regex": r"paid=(\d+)"}}


def test_the_metric_is_read_from_the_matching_item_and_compared_with_the_computed_value():
    before = P.read_metric(PROBE, Client({"/goals": {"goals": [{"title": "Other", "description": "Current value: 9"}, {"title": "Reach 1000", "description": "Current value: 1,204"}]},
                                          "/funnel": "paid=1204"}))
    assert before == {"name": "paid users", "value": 1204.0, "computed": 1204.0}
    after = {"name": "paid users", "value": 1210.0, "computed": 1210.0}
    block = P.metric_verdict(PROBE, before, after)
    assert (block["before"], block["after"], block["delta"], block["moved"], block["matches_computed"]) == (1204.0, 1210.0, 6.0, "yes", True)
    assert P.metric_verdict(PROBE, before, dict(after, computed=1300.0))["matches_computed"] is False


def test_a_metric_that_did_not_change_is_never_called_moved():
    same = {"value": 5.0}
    assert P.metric_verdict({"name": "m"}, same, same)["moved"] == "no"
    assert P.metric_verdict({"name": "m", "movable": False}, same, {"value": 9.0})["moved"] == P.NOT_MOVABLE
    assert P.metric_verdict({"name": "m"}, same, {"value": 9.0}, "owned next step; the metric cannot move in one turn")["moved"] == P.NOT_MOVABLE
    assert P.metric_verdict({"name": "m"}, {"value": None}, same)["moved"] == "unreadable"


def test_messages_are_every_assistant_message_in_order_and_status_ticks_are_not_messages():
    frames = [{"type": "status", "text": "Planning"}, {"type": "interim", "text": "Status: 1,204 of 1,000. Reading the funnel now.", "t": 3.0},
              {"type": "milestone", "text": "Read the funnel", "t": 20.1}, {"type": "milestone", "text": "Read the funnel"},
              {"type": "result", "text": "x"}, {"type": "done", "step": 2}, {"type": "done", "content": "Done: added the step. Before 1,204, after 1,204.", "t": 41.0}]
    messages = P.messages_of(frames)
    assert [(m["n"], m["kind"], m["t"]) for m in messages] == [(1, "interim", 3.0), (2, "milestone", 20.1), (3, "final", 41.0)]


def test_an_action_case_gets_the_two_role_standard_and_an_information_case_one_message():
    action = {"id": "M-1", "group": "milestone", "effect": "one goal"}
    info = {"id": "B-1", "group": "production failure", "effect": "none"}
    note, item = P.sequence_note(action, [{"n": 1, "kind": "interim", "t": 2.0, "text": "a"}, {"n": 2, "kind": "final", "t": 9.0, "text": "b"}],
                                 {"name": "m", "before": 1, "after": 1, "delta": 0, "moved": "no"})
    assert "message 1" in item and "message 2" in item and "(1, interim, t=2.0s) a | (2, final, t=9.0s) b" in note and "moved no" in note
    assert P.sequence_note(info, [{"n": 1, "kind": "final", "t": None, "text": "b"}])[1] == P.SINGLE_MESSAGE_ITEM
    assert P.is_action_case({"id": "x", "effect": "workflow on the quest"}) and not P.is_action_case({"id": "x", "effect": "none (reply only)"})


def test_structured_rules_from_a_file_are_laid_over_prose_and_never_over_a_structured_value():
    rules = {"M-1": {"effect": {"allow": [{"tool": "t"}]}, "state_check": {"read": "/x", "expect": 1}}}
    merged = P.with_rules({"id": "M-1", "effect": "prose", "state_check": {"read": "/mine"}}, rules)
    assert merged["effect"] == {"allow": [{"tool": "t"}]} and merged["state_check"] == {"read": "/mine"}
    assert P.with_rules({"id": "other", "effect": "prose"}, rules)["effect"] == "prose"
