"""Structure in the reply: the planner's structured move reaches the reply only through the records port, a plain turn stays plain, the facts come from what the records
hold (never a made-up value, and a held metric is never replaced), at most one offer is made and parked by key, an accepted or declined offer is carried out by code, and the
closing paragraph of a framed or compared reply is appended by code and never loses the reply."""
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.mind.orchestrator_types import OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decide_tool import DECIDE_TOOL
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import normalize_decision
from quest_ai_runner.the_intelligence.mind.response_writer import structure_reply as module
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import sentences
from quest_ai_runner.the_intelligence.mind.response_writer.structure_reply import finish_structure, handle_offer, open_offer_for, settle_offer, snapshot_sentence, solution_choice, structure_reply, tail_prompt

RECORDS = {"problems": {"p": {"name": "A problem"}}, "ideals": {"i": {"name": "An ideal"}}}
HELD = dict(RECORDS, solutions={"s": {"name": "A solution", "problem_ids": ["p"], "ideal_ids": ["i"], "milestones": "1. A target"}},
            implementations={"a": {"solution_id": "s", "name": "Held option"}},
            metrics={"m": {"name": "A measure", "solution_ids": ["s"], "record": "Current value: 4 (as of 2026-11-03, from the person's own report)"}})
SEVERAL = dict(HELD, solutions=dict(HELD["solutions"], t={"name": "Another solution", "problem_ids": ["p"], "ideal_ids": ["i"]}))
META = {"quest_ids": ["q1"], "conversation_id": "c1"}


class Port:
    def __init__(self, records, mode="auto", declined=(), open_offer=None):
        self.records, self.mode, self.declined_keys, self.open, self.parked, self.calls = records, mode, list(declined), open_offer, [], []

    def read(self, subject):
        return self.records

    def write_mode(self, subject):
        return self.mode

    def declined(self, subject):
        return self.declined_keys

    def park_offer(self, subject, conversation_id, offer, reply):
        self.parked.append((subject, conversation_id, dict(offer), reply))
        return "offer-1"

    def open_offer(self, subject, conversation_id):
        return self.open

    def accept_offer(self, offer_id):
        self.calls.append(("accept", offer_id))
        return {"status": "applied", "message": "Done: kept alternatives."}

    def decline_offer(self, offer_id):
        self.calls.append(("decline", offer_id))


def plan(move, **fields):
    return SimpleNamespace(**dict({"structure_move": move, "action": "answer", "asks_first": False, "reply_shape": "advice", "delivery": None, "accepts_offer": False,
                                   "declines_offer": False}, **fields))


def test_the_planner_schema_offers_the_moves_and_the_offer_fields():
    properties = DECIDE_TOOL["input_schema"]["properties"]
    assert set(properties["structure_move"]["enum"]) == {"none", "frame", "compare", "review", "focus"}
    assert properties["accepts_offer"]["type"] == "boolean" and properties["declines_offer"]["type"] == "boolean"
    cfg = OrchestratorConfig()
    assert normalize_decision({"action": "answer", "rationale": "r"}, cfg).structure_move is None
    assert normalize_decision({"action": "answer", "rationale": "r", "structure_move": "frame"}, cfg).structure_move == "frame"
    assert normalize_decision({"action": "answer", "rationale": "r", "structure_move": "banter"}, cfg).structure_move is None
    got = normalize_decision({"action": "answer", "rationale": "r", "accepts_offer": True}, cfg)
    assert got.accepts_offer is True and got.declines_offer is False


def test_a_plain_turn_gets_no_structure():
    assert structure_reply(plan("none"), META, Port(HELD), True) is None
    assert structure_reply(plan("frame", reply_shape="readback"), META, Port(HELD), True) is None
    assert structure_reply(plan("frame", delivery="spoken"), META, Port(HELD), True) is None
    assert structure_reply(plan("frame"), META, None, True) is None
    assert structure_reply(plan("frame"), {"quest_ids": ["a", "b"]}, Port(HELD), True) is None
    assert structure_reply(plan("frame"), {}, Port(HELD), True) is None


def test_a_bare_quest_offers_the_missing_pieces_with_a_key():
    got = structure_reply(plan("frame"), META, Port(RECORDS), True)
    assert "A problem" in got.facts and got.directive.startswith("--- THE REPLY ENDS WITH THE LAST PRACTICE") and "The ideal: An ideal" in got.facts
    assert got.offer["pieces"] == ["solution", "alternatives", "metric", "milestone"] and got.offer["key"] == "structure:new:solution,alternatives,metric,milestone"
    assert "holds none of this structure yet" in structure_reply(plan("frame"), META, Port({}), True).facts


def test_held_structure_is_used_first_and_a_held_metric_is_never_replaced():
    got = structure_reply(plan("frame"), META, Port(HELD), True)
    assert "latest value 4 as of 2026-11-03" in got.facts and "never propose another" in got.facts and "Held option" in got.facts
    assert got.offer["pieces"] == ["alternatives"] and got.block["solution"]["existing"]
    assert "a held metric is never replaced" in tail_prompt("frame", "r", got.facts, got.pieces)


def test_several_solutions_use_the_one_the_relevance_arm_picks_and_none_fits_proposes_a_new_one(monkeypatch):
    class Arm:
        def __init__(self, plumbing, tier):
            pass

        def relevant(self, message, network, limit):
            return {"solutions": [{"solution_id": "t"}]}

    monkeypatch.setattr(module, "RecordsThenModel", Arm)
    monkeypatch.setattr(module.SolutionNetwork, "from_rows", classmethod(lambda cls, rows: object()))
    got = structure_reply(plan("frame"), META, Port(SEVERAL), True, "a question", lambda prompt: "")
    assert got.block["solution"]["name"] == "Another solution" and got.offer["key"].startswith("structure:t:")

    class NoneFits(Arm):
        def relevant(self, message, network, limit):
            return {"solutions": []}
    monkeypatch.setattr(module, "RecordsThenModel", NoneFits)
    got = structure_reply(plan("frame"), META, Port(SEVERAL), True, "a question", lambda prompt: "")
    assert got.block["solution"] is None and "solution" in got.offer["pieces"] and "would be proposed" in got.facts
    assert solution_choice(SEVERAL, "a question", None) == "" and solution_choice(HELD) == "s"


def test_the_gate_and_a_declined_key_leave_no_offer():
    assert structure_reply(plan("frame"), META, Port(HELD, mode=""), True).offer is None
    assert structure_reply(plan("frame"), META, Port(HELD, mode="ask_first"), True).offer["mode"] == "ask_first"
    declined = structure_reply(plan("frame"), META, Port(HELD, declined=["structure:s:alternatives"]), True)
    assert declined.offer is None and "Make no offer" in tail_prompt("frame", "r", declined.facts, declined.pieces)


def test_review_and_focus_are_written_from_the_records_and_a_snapshot_turn_gets_the_facts_only():
    records = dict(HELD, metrics={"m": {"name": "A measure", "solution_ids": ["s"], "record": "Counts per week."}})
    got = structure_reply(plan("review", reply_shape="default"), META, Port(records), True)
    assert got.offer is None and "no value logged yet" in got.directive and "answer from the records" in got.directive
    snapshot = structure_reply(plan("review", reply_shape=None), META, Port(HELD), True, snapshot=True)
    assert snapshot.directive == "" and snapshot.snapshot and snapshot.offer is None
    assert snapshot_sentence("review", snapshot.block) == ("The metric that shows whether this is working is A measure, last recorded as 4 on 2026-11-03. The next milestone is A target.")
    focus = structure_reply(plan("focus", reply_shape=None), META, Port(HELD), True, snapshot=True)
    assert snapshot_sentence("focus", focus.block).startswith("The problem standing in your way is: A problem.")
    rs = SimpleNamespace(structure=snapshot, _ctx_meta=META)
    res = SimpleNamespace(kind="answer", text="Where it stands.")
    assert finish_structure(Orch(Port(HELD)), rs, res) and res.text.startswith("Where it stands. The metric that shows") and not Port(HELD).parked


class Orch:
    def __init__(self, port):
        self.registry = SimpleNamespace(resolve_role=lambda call: "m")
        self.provider, self.cfg = object(), SimpleNamespace(structure_records=port)


def test_the_closing_paragraph_is_appended_the_offer_is_parked_and_a_failure_leaves_the_answer(monkeypatch):
    port = Port(HELD)
    rs = SimpleNamespace(structure=structure_reply(plan("frame"), META, port, True), _ctx_meta=META)
    res = SimpleNamespace(kind="answer", text="The answer.")
    monkeypatch.setattr(module, "answer_with_reasoning", lambda *a, **k: "This serves the problem. Keep these?")
    assert finish_structure(Orch(port), rs, res) and res.text == "The answer.\n\nThis serves the problem. Keep these?"
    subject, conv, offer, reply = port.parked[0]
    assert (subject, conv, offer["key"], offer["solution_id"]) == ("q1", "c1", "structure:s:alternatives", "s") and reply == res.text
    assert rs.structure.block["offer"]["id"] == "offer-1"

    def fail(*a, **k):
        raise RuntimeError("down")
    port = Port(HELD)
    rs = SimpleNamespace(structure=structure_reply(plan("frame"), META, port, True), _ctx_meta=META)
    res = SimpleNamespace(kind="answer", text="The answer.")
    monkeypatch.setattr(module, "answer_with_reasoning", fail)
    assert not finish_structure(Orch(port), rs, res) and res.text == "The answer." and port.parked
    assert not finish_structure(Orch(port), SimpleNamespace(structure=None), res)


def test_an_alternatives_tail_already_written_this_turn_means_no_second_path(monkeypatch):
    port = Port(HELD)
    rs = SimpleNamespace(structure=structure_reply(plan("frame"), META, port, True), _ctx_meta=META, alternatives_kept=True)
    res = SimpleNamespace(kind="answer", text="The answer.")
    monkeypatch.setattr(module, "answer_with_reasoning", lambda *a, **k: "never used")
    assert not finish_structure(Orch(port), rs, res) and res.text == "The answer."


def test_an_open_offer_is_accepted_or_declined_by_code_from_the_planners_structured_fields():
    offer = {"id": "offer-1", "key": "structure:s:alternatives", "pieces": ["alternatives"]}
    port = Port(HELD, open_offer=offer)
    assert open_offer_for(META, port, True) == offer and open_offer_for(META, port, False) is None and open_offer_for({}, port, True) is None
    accepted = handle_offer(plan("none", accepts_offer=True), SimpleNamespace(open_offer=offer), port)
    assert port.calls == [("accept", "offer-1")] and "Done: kept alternatives." in accepted
    declined = handle_offer(plan("none", declines_offer=True), SimpleNamespace(open_offer=offer), port)       # a new turn: its own state
    assert port.calls[-1] == ("decline", "offer-1") and "DECLINED" in declined and len(port.calls) == 2
    rs = SimpleNamespace(open_offer=offer)
    assert handle_offer(plan("none"), rs, port) == "" and handle_offer(plan("none", accepts_offer=True), SimpleNamespace(open_offer=None), port) == ""
    assert len(port.calls) == 2


def test_a_decline_never_writes_records_and_contradictory_or_repeated_signals_do_nothing_more():
    offer = {"id": "offer-1", "key": "k", "pieces": ["alternatives"]}
    both = Port(HELD, open_offer=offer)
    rs = SimpleNamespace(open_offer=offer)
    assert handle_offer(plan("none", accepts_offer=True, declines_offer=True), rs, both) == "" and both.calls == [] and not getattr(rs, "offer_receipt", "")
    declined = Port(HELD, open_offer=offer)
    rs = SimpleNamespace(open_offer=offer)
    handle_offer(plan("none", declines_offer=True), rs, declined)
    assert declined.calls == [("decline", "offer-1")]
    handle_offer(plan("none", accepts_offer=True), rs, declined)           # a later pass of the same turn with an accept signal: the decline stands, nothing is kept
    handle_offer(plan("none", accepts_offer=True, declines_offer=True), rs, declined)
    assert declined.calls == [("decline", "offer-1")] and "will not offer" in rs.offer_receipt
    accepted = Port(HELD, open_offer=offer)
    rs = SimpleNamespace(open_offer=offer)
    handle_offer(plan("none", accepts_offer=True), rs, accepted)
    handle_offer(plan("none", declines_offer=True), rs, accepted)          # and an accept is never followed by a decline in the same turn
    assert accepted.calls == [("accept", "offer-1")]


def test_the_reply_to_an_accepted_or_declined_offer_is_the_sentence_code_composed():
    offer = {"id": "offer-1", "key": "k", "pieces": ["alternatives"]}
    port = Port(HELD, open_offer=offer)
    rs = SimpleNamespace(open_offer=offer)
    handle_offer(plan("none", accepts_offer=True), rs, port)
    res = SimpleNamespace(kind="answer", text="The model claimed more than was done.")
    assert settle_offer(rs, res) and res.text == "Done: kept alternatives."
    rs = SimpleNamespace(open_offer=offer)
    handle_offer(plan("none", declines_offer=True), rs, port)
    res = SimpleNamespace(kind="answer", text="x")
    assert settle_offer(rs, res) and "will not offer" in res.text
    assert not settle_offer(SimpleNamespace(), SimpleNamespace(kind="answer", text="x"))


LONG = ("This practice serves the intent's core problem, which is that with no way to know whether a strategy works your time is wasted and your motivation fades, and what you are "
        "aiming for is objective measurement that shows you real progress, so the practices above are alternatives to compare and not a checklist to finish and you should try them side "
        "by side and let your measured development rate tell you which one moves the needle, because that rate has no value logged yet and your first reading will set the baseline.")
SHORT = ("The problem this serves is wasted time, and the ideal here is visible progress. The alternatives above are different ways to carry out the solution of measuring, and the "
         "metric for them is Hit rate, currently no value logged yet. The next milestone is ten logged sessions.")


def test_the_closing_paragraph_is_held_to_three_short_sentences_by_the_reply_standards_measure(monkeypatch):
    prompts = []

    def call(prompt):
        prompts.append(prompt)
        return (LONG if len(prompts) == 1 else SHORT) + "\nShall I keep these alternatives?"
    got = module.measured_tail(call, "frame", "The answer.", "facts", ["alternatives"])
    assert got == SHORT + " Shall I keep these alternatives?" and len(prompts) == 2 and "reviewer flagged your earlier try" in prompts[1] and "cut it to under 420 characters" in prompts[1]
    stubborn = module.measured_tail(lambda prompt: LONG, "frame", "The answer.", "facts", [], rounds=1)
    assert len(sentences(stubborn)) <= 3 and len(stubborn) <= 420 and stubborn
    assert module.tail_lines("One.\nTwo.\nThree.", []) == ["One."] and module.tail_lines("One.\nTwo.", ["alternatives"]) == ["One.", "Two."] and module.tail_lines("", []) == []


def test_the_term_rules_name_the_six_words_and_the_one_home_of_the_closing():
    prompt = tail_prompt("frame", "r", "facts", ["alternatives"])
    for word in ("problem", "ideal", "solution", "alternatives", "metric", "milestone"):
        assert word in module.TERM_RULES
    assert "never \"strategy\" or \"approach\"" in prompt and "name the held metric once by exactly its own name" in prompt and "It is the only offer of the reply." in prompt
    assert "no offer of more details" in module.CLOSING_RULE and "—" not in prompt
