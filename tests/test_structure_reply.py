"""Structure in the reply: the planner's structured move reaches the reply only through the records port, a plain turn stays plain, the facts come from what the records
hold (never a made-up value), at most one offer is made, and the closing paragraph of a framed or compared reply is appended by code and never loses the reply."""
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.mind.orchestrator_types import OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decide_tool import DECIDE_TOOL
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import normalize_decision
from quest_ai_runner.the_intelligence.mind.response_writer import structure_reply as module
from quest_ai_runner.the_intelligence.mind.response_writer.structure_reply import append_structure, structure_reply, tail_prompt

RECORDS = {"problems": {"p": {"name": "A problem"}}, "ideals": {"i": {"name": "An ideal"}}}
HELD = dict(RECORDS, solutions={"s": {"name": "A solution", "problem_ids": ["p"], "ideal_ids": ["i"], "milestones": "1. A target"}},
            implementations={"a": {"solution_id": "s", "name": "Held option"}},
            metrics={"m": {"name": "A measure", "solution_ids": ["s"], "record": "Current value: 4 (as of 2026-11-03, from the person's own report)"}})
META = {"quest_ids": ["q1"]}


class Port:
    def __init__(self, records, writable=True):
        self.records, self.writable = records, writable

    def read(self, subject):
        return self.records

    def can_write(self, subject):
        return self.writable


def plan(move, **fields):
    return SimpleNamespace(**dict({"structure_move": move, "action": "answer", "asks_first": False, "reply_shape": "advice", "delivery": None}, **fields))


def test_the_planner_schema_offers_the_moves_and_a_missing_move_is_none():
    assert set(DECIDE_TOOL["input_schema"]["properties"]["structure_move"]["enum"]) == {"none", "frame", "compare", "review", "focus"}
    cfg = OrchestratorConfig()
    assert normalize_decision({"action": "answer", "rationale": "r"}, cfg).structure_move is None
    assert normalize_decision({"action": "answer", "rationale": "r", "structure_move": "frame"}, cfg).structure_move == "frame"
    assert normalize_decision({"action": "answer", "rationale": "r", "structure_move": "banter"}, cfg).structure_move is None


def test_a_plain_turn_gets_no_structure():
    assert structure_reply(plan("none"), META, Port(HELD), True) is None
    assert structure_reply(plan("frame", reply_shape="readback"), META, Port(HELD), True) is None
    assert structure_reply(plan("frame", delivery="spoken"), META, Port(HELD), True) is None
    assert structure_reply(plan("frame"), META, None, True) is None
    assert structure_reply(plan("frame"), {"quest_ids": ["a", "b"]}, Port(HELD), True) is None
    assert structure_reply(plan("frame"), {}, Port(HELD), True) is None


def test_a_bare_quest_offers_the_missing_pieces_and_a_framed_reply_has_no_directive():
    got = structure_reply(plan("frame"), META, Port(RECORDS), True)
    assert "A problem" in got.facts and "The approach already chosen" not in got.facts and got.directive == ""
    assert got.offer["pieces"] == ["solution", "alternatives", "metric", "milestone"]
    assert "holds none of this structure yet" in structure_reply(plan("frame"), META, Port({}), True).facts


def test_held_structure_is_used_first_and_the_value_is_the_records():
    got = structure_reply(plan("frame"), META, Port(HELD), True)
    assert "latest value 4 as of 2026-11-03" in got.facts and "Held option" in got.facts
    assert got.offer["pieces"] == ["alternatives"] and got.block["solution"]["existing"]


def test_review_is_written_from_the_records_and_never_offers():
    records = dict(HELD, metrics={"m": {"name": "A measure", "solution_ids": ["s"], "record": "Counts per week."}})
    got = structure_reply(plan("review", reply_shape="default"), META, Port(records), True)
    assert got.offer is None and "no value logged yet" in got.directive and "answer from the records" in got.directive


def test_no_offer_when_the_person_settings_do_not_let_it_be_kept():
    got = structure_reply(plan("frame"), META, Port(HELD, writable=False), True)
    assert got.offer is None and "write no offer" not in tail_prompt("frame", "r", got.facts, got.pieces) and "Make no offer" in tail_prompt("frame", "r", got.facts, got.pieces)


def test_several_solutions_name_none_and_offer_no_second_one():
    records = dict(HELD, solutions=dict(HELD["solutions"], t={"name": "Another solution"}))
    got = structure_reply(plan("frame"), META, Port(records), True)
    assert got.offer is None and "Another solution" in got.facts and got.block["solution"] is None


class Orch:
    def __init__(self, text):
        self.registry = SimpleNamespace(resolve_role=lambda call: "m")
        self.provider, self.text = object(), text


def test_the_closing_paragraph_is_appended_to_a_framed_answer_and_a_failure_leaves_the_answer(monkeypatch):
    rs = SimpleNamespace(structure=structure_reply(plan("frame"), META, Port(HELD), True))
    res = SimpleNamespace(kind="answer", text="The answer.")
    monkeypatch.setattr(module, "answer_with_reasoning", lambda *a, **k: "This serves the problem. Keep these?")
    assert append_structure(Orch(""), rs, res) and res.text == "The answer.\n\nThis serves the problem. Keep these?"

    def fail(*a, **k):
        raise RuntimeError("down")
    res = SimpleNamespace(kind="answer", text="The answer.")
    monkeypatch.setattr(module, "answer_with_reasoning", fail)
    assert not append_structure(Orch(""), rs, res) and res.text == "The answer."
    assert not append_structure(Orch(""), SimpleNamespace(structure=None), res)
