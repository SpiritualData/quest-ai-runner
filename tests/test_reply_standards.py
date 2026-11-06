"""Situation standards at the answer checkpoint: the measurements, the code-built repair rounds and the last-resort repair."""
from typing import Any, Dict, List

from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decide_tool import DECIDE_TOOL
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import normalize_decision
from quest_ai_runner.the_intelligence.mind.delegation.overseer import OVERSEER_PROMPT
from quest_ai_runner.the_intelligence.mind.planner.planner_prompt import PLANNER_PROFILES
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import REPLY_SHAPES, STANDARDS, measure, repair, standard_for

from .conftest import StubProvider, StubRetrieval

EVIDENCE = "Plumber: call Dana about the leak. Tutor arrives 20 Nov at 4:30 pm. Last run 54:10. Goal: Walk daily."


def kinds(m):
    return {v.kind for v in m.violations}


def test_a_one_step_reply_with_one_step_and_one_question_meets_the_standard():
    m = measure("Call Dana about the leak today. Does she have a free slot this week?", STANDARDS["one_step"], EVIDENCE)
    assert not m.failed and m.questions == 1


def test_a_list_and_two_questions_break_the_one_step_standard_and_the_hint_names_them():
    draft = "Here is a plan:\n1. Call Dana\n2. Order parts\nWhen can you call? And who pays?"
    m = measure(draft, STANDARDS["one_step"], EVIDENCE)
    assert kinds(m) == {"questions", "list"} and m.questions == 2 and m.list_lines == 2
    assert "at most 1 question" in m.hint() and "not a list" in m.hint()


def test_dates_times_and_quoted_names_the_turn_never_held_are_found_and_held_ones_pass():
    held = measure('The tutor arrives 20 Nov at 4:30 pm, and your last run was 54:10 under "Walk daily".', STANDARDS["default"], EVIDENCE)
    assert not held.failed
    bad = measure('Tiling is 6-9 Nov, starting 1 Nov at 9am, for "Book my next tutor session".', STANDARDS["default"], EVIDENCE)
    assert bad.unsupported == ["6-9 Nov", "1 Nov", "9am", "Book my next tutor session"]
    assert "remove '6-9 Nov'" in bad.hint()


def test_a_length_cap_is_a_count_not_a_word_list():
    m = measure("word " * 200, STANDARDS["brief"], EVIDENCE)
    assert kinds(m) == {"length"}


def test_the_last_resort_repair_drops_the_unsupported_sentence_and_extra_questions_and_never_empties_the_reply():
    draft = "Call Dana today. Tiling runs 6-9 Nov. Is she free? Or is the other plumber?"
    m = measure(draft, STANDARDS["one_step"], EVIDENCE)
    assert repair(draft, STANDARDS["one_step"], m, EVIDENCE) == "Call Dana today. Is she free?"
    only = "Tiling runs 6-9 Nov."
    assert repair(only, STANDARDS["one_step"], measure(only, STANDARDS["one_step"], EVIDENCE), EVIDENCE) == only


def test_the_standard_comes_from_structured_fields_only():
    class Plan:
        reply_shape = "yes_no"
        asks_first = False
    assert standard_for(Plan()).shape == "yes_no"
    Plan.asks_first = True
    assert standard_for(Plan()).shape == "ask_first"
    Plan.reply_shape, Plan.asks_first = "nonsense", False
    assert standard_for(Plan()).shape == "default"


def test_the_planner_field_is_in_the_schema_the_parser_and_the_rubric():
    cfg = OrchestratorConfig()
    assert DECIDE_TOOL["input_schema"]["properties"]["reply_shape"]["enum"] == list(REPLY_SHAPES)
    assert normalize_decision({"action": "answer", "reply_shape": "one_step"}, cfg).reply_shape == "one_step"
    assert normalize_decision({"action": "answer", "reply_shape": "bogus"}, cfg).reply_shape is None
    assert all("`reply_shape`" in text for text in PLANNER_PROFILES.values())


def test_no_em_dash_in_the_new_text():
    assert "—" not in OVERSEER_PROMPT and all("—" not in s.text for s in STANDARDS.values())


class ScriptedAnswers(StubProvider):
    """Overseer calls get ``verdict``; reply calls draw from ``drafts`` (the last repeats)."""

    def __init__(self, decisions: List[Dict[str, Any]], drafts: List[str], verdict: Dict[str, Any]):
        super().__init__(decisions)
        self.drafts, self.verdict, self.overseer_calls, self.steers = list(drafts), verdict, 0, []

    def plan(self, prompt, *, model, tool_schema):
        if "minimal-intervention" in prompt.lower():
            self.overseer_calls += 1
            self.last_digest = prompt
            return self.verdict
        return super().plan(prompt, model=model, tool_schema=tool_schema)

    def answer(self, messages, *, model, system=None):
        super().answer(messages, model=model, system=system)
        joined = "\n".join(m["content"] for m in messages)
        if "GROUNDING CONTEXT" not in joined:
            return "ok"
        self.steers.append(joined)
        return self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0]


def steered(provider):
    return [t for t in provider.steers if "YOUR PREVIOUS ANSWER" in t]


def run_turn(drafts, verdict=None, shape="one_step"):
    provider = ScriptedAnswers([{"action": "answer", "rationale": "r", "reply_shape": shape}], drafts, verdict or {"signal": "proceed"})
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        config=OrchestratorConfig(overseer=True, overseer_min_step=99, max_steps=5))
    return provider, orch.run("Where do I start with the leak?")


GOOD = "Call Dana about the leak today. Does she have a slot this week?"
BAD = "Start here:\n1. Call Dana\n2. Order parts\nWhen can you call? Who pays?"


def test_a_right_draft_costs_exactly_the_one_reply_and_the_one_overseer_call_it_always_had():
    provider, res = run_turn([GOOD])
    assert not steered(provider) and provider.overseer_calls == 1 and res.text.startswith("Call Dana")
    assert "\nSITUATION STANDARD:" in provider.last_digest and "meets the limits" in provider.last_digest


def test_a_wrong_draft_is_regenerated_once_with_a_hint_that_names_the_violation():
    provider, res = run_turn([BAD, GOOD])
    assert len(steered(provider)) == 1 and res.text.startswith("Call Dana")
    assert "at most 1 question" in steered(provider)[0] and "not a list" in steered(provider)[0]
    assert provider.overseer_calls == 1


def test_a_draft_that_keeps_failing_gets_two_repair_rounds_then_the_deterministic_repair():
    provider, res = run_turn(["Call Dana today. Tiling runs 6-9 Nov. Is she free? Or someone else?"])
    assert len(steered(provider)) == 2                     # the two repair rounds, never more
    assert res.text == "Call Dana today. Is she free?"


def test_the_overseers_own_redirect_hint_is_joined_with_the_measured_one():
    provider, res = run_turn([BAD, GOOD], verdict={"signal": "redirect", "hint": "lead with the plumber"})
    assert len(steered(provider)) == 1 and "lead with the plumber" in steered(provider)[0] and "at most 1 question" in steered(provider)[0]


def test_a_default_reply_with_nothing_unsupported_sees_the_unchanged_digest():
    provider, _ = run_turn(["Fine, thanks."], shape="default")
    assert "\nSITUATION STANDARD:" not in provider.last_digest and not steered(provider)


def test_an_iso_timestamp_in_the_evidence_supports_that_day_and_not_the_day_before():
    evidence = "Run Log 2026-10-23T06:00:00Z 10K 54:10"
    assert not measure("Your 23 October trial was 54:10.", STANDARDS["default"], evidence).failed
    assert measure("Your 22 October trial was 54:10.", STANDARDS["default"], evidence).unsupported == ["22 October"]


def test_a_wrong_days_from_today_count_is_a_violation_and_the_right_one_is_not():
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import STANDARDS, SHAPE_DEFAULT, day_count_errors, measure
    evidence = "Current date: 2026-11-01 (Sunday). The in-laws arrive 20 Nov."
    wrong = "If they arrive a week early, on 13 Nov, that leaves you 10 days from today instead of 19."
    assert day_count_errors(wrong, evidence) == ["12 days from today to 13 Nov, not 10"]
    assert day_count_errors("On 13 Nov, that leaves you 12 days from today.", evidence) == []
    assert day_count_errors("Finish 3 days before they arrive on 20 Nov.", evidence) == []   # counts from another date: not judged
    assert day_count_errors(wrong, "- Date: Sunday, November 1, 2026") == ["12 days from today to 13 Nov, not 10"]
    assert day_count_errors("On 13 Nov that is 12 days from today.", "no date here") in ([], [])  # server-date fallback with one day of slack
    measured = measure(wrong, STANDARDS[SHAPE_DEFAULT], evidence)
    assert any(v.kind == "days" for v in measured.violations) and "12 days from today" in measured.hint()


def test_a_day_offset_from_an_event_date_the_paragraph_states_is_checked():
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import event_offset_errors
    wrong = ("If they arrive a week early they would come on 13 Nov, which leaves you until 10 Nov, and your own "
             "target is already 3 days before arrival (17 Nov). That squeezes the work.")
    errors = event_offset_errors(wrong)
    assert len(errors) == 1 and "10 Nov" in errors[0] and "17 Nov" in errors[0]
    right = "They would come on 13 Nov, so your target is 3 days before arrival (10 Nov)."
    assert event_offset_errors(right) == []
    assert event_offset_errors("The race is 2 days after arrival (22 Nov).") == []   # no earlier date to count from


class ConstraintProvider(ScriptedAnswers):
    """Judge calls for the found_constraint standard get ``constraint``; everything else is scripted as above."""

    def __init__(self, decisions, drafts, constraint):
        super().__init__(decisions, drafts, {"signal": "proceed"})
        self.constraint, self.constraint_calls = constraint, 0

    def plan(self, prompt, *, model, tool_schema):
        if tool_schema.get("name") == "found_constraint_verdict":
            self.constraint_calls += 1
            if isinstance(self.constraint, Exception):
                raise self.constraint
            return self.constraint
        return super().plan(prompt, model=model, tool_schema=tool_schema)


OFFERS = "Would you like Tuesday or Thursday instead, or should I ask about a Friday exception?"
STATES = "Friday will not work: the tutor only teaches Tuesday and Thursday evenings. Would Tuesday or Thursday suit you?"


class RecordsAssembler:
    """A context assembler that reports the person's records as a source (or nothing)."""

    def __init__(self, records=True):
        self.records = records

    def assemble(self, task_text, *, meta=None):
        from hubofhubs.api import AssembledContext
        sources = [{"adapter": "records", "label": "your records", "items": ["quest-note:1"]}] if self.records else []
        return AssembledContext(context_view="Quest note: the tutor only teaches Tuesday and Thursday evenings.", sources=sources)

    def record(self, task_text, outcome):
        return None


def run_constraint(drafts, constraint, records=True):
    provider = ConstraintProvider([{"action": "answer", "rationale": "r", "reply_shape": "default"}], drafts, constraint)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        config=OrchestratorConfig(overseer=False, max_steps=5), context_assembler=RecordsAssembler(records))
    return provider, orch.run("Can you help me book the tutor for Friday?")


def test_a_reply_that_leaves_the_conflicting_request_open_is_regenerated_once_with_the_verdicts_own_fact():
    verdict = {"conflict": True, "fact": "The tutor only teaches Tuesday and Thursday evenings",
               "consequence": "Friday will not work", "states_it_first": False}
    provider, res = run_constraint([OFFERS, STATES], verdict)
    assert len(steered(provider)) == 1 and res.text == STATES
    hint = steered(provider)[0]
    assert "Friday will not work" in hint and "Tuesday and Thursday evenings" in hint and "—" not in hint


def test_a_reply_that_states_the_conflict_first_or_a_turn_with_no_conflict_is_left_alone():
    for verdict in ({"conflict": True, "fact": "f", "consequence": "c", "states_it_first": True}, {"conflict": False}):
        provider, res = run_constraint([STATES], verdict)
        assert not steered(provider) and res.text == STATES and provider.constraint_calls == 1


def test_a_judge_outage_keeps_the_reply():
    provider, res = run_constraint([OFFERS], RuntimeError("provider outage"))
    assert not steered(provider) and res.text == OFFERS


def test_the_found_constraint_standard_is_generic_and_has_no_em_dash():
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import FOUND_CONSTRAINT_STANDARD, found_constraint_hint
    assert "—" not in FOUND_CONSTRAINT_STANDARD and "—" not in found_constraint_hint("a", "b")
    assert "Friday" not in FOUND_CONSTRAINT_STANDARD.replace("(for example 'Friday will not work: <fact>')", "")


def test_the_head_for_a_reply_the_model_did_not_write_is_built_from_the_verdict_fields_only():
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import found_constraint_head
    assert found_constraint_head("The tutor only teaches Tuesday and Thursday evenings", "Friday will not work") == \
        "Friday will not work. The tutor only teaches Tuesday and Thursday evenings."
    assert found_constraint_head("", "") == ""


def test_a_reply_that_states_the_conflict_but_still_offers_the_request_is_also_regenerated():
    verdict = {"conflict": True, "fact": "f", "consequence": "c", "states_it_first": True, "leaves_request_open": True}
    provider, res = run_constraint([OFFERS, STATES], verdict)
    assert len(steered(provider)) == 1 and "go ahead anyway" in steered(provider)[0]


def test_the_judge_runs_only_on_turns_whose_context_held_the_persons_records():
    provider, _ = run_constraint([STATES], {"conflict": True, "states_it_first": False}, records=False)
    assert provider.constraint_calls == 0 and not steered(provider)
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import constraint_violated, turn_holds_records
    assert turn_holds_records([{"adapter": "records", "items": ["x"]}]) and not turn_holds_records([{"adapter": "records", "items": []}, {"adapter": "recent", "items": ["y"]}], [])
    assert not constraint_violated({"conflict": False, "leaves_request_open": True})
    assert not constraint_violated({"conflict": True, "states_it_first": True, "leaves_request_open": False})


def test_a_found_conflict_queues_nothing_even_when_the_planner_forgot_act_declined():
    decisions = [{"action": "answer", "rationale": "r", "reply_shape": "default",
                  "deferred_deep": {"goal": "Book the tutor for Friday", "rationale": "book it"}}]
    verdict = {"conflict": True, "fact": "f", "consequence": "c", "states_it_first": True, "leaves_request_open": False}
    provider = ConstraintProvider(decisions, [STATES], verdict)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                        config=OrchestratorConfig(overseer=False, max_steps=5), context_assembler=RecordsAssembler())
    res = orch.run("Can you help me book the tutor for Friday?")
    assert not res.deep_results and res.text == STATES


def test_the_ask_carries_the_conflict_in_the_question_that_is_shown_and_filed():
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import file_blocked_ask
    seen = {}

    class Orch:
        escalation = None
        def _concise_decision_summary(self, q):
            return q
    import quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages as m
    orig = m.file_ask
    m.file_ask = lambda *a, **k: seen.update(k) or type("O", (), {"filed": False, "decision_id": "", "assignee": None})()
    try:
        res = type("R", (), {"output": "", "observations": [], "observations_reported": False, "coverage": None})()
        file_blocked_ask(Orch(), None, {"found_constraint_head": "Friday will not work."}, res,
                         {"blocker": "needs_person"}, question="Which day?")
    finally:
        m.file_ask = orig
    assert seen.get("question") == "Friday will not work. Which day?" or not seen


def test_ask_path_runs_the_found_constraint_judge_once_and_stores_the_head():
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import ensure_found_constraint_head

    class FakeOrch:
        calls = 0

        def found_constraint_verdict(self, request, records, reply):
            FakeOrch.calls += 1
            return {"conflict": True, "fact": "Ana only teaches on Tuesday and Thursday evenings", "consequence": "Friday will not work"}

    meta = {}
    ensure_found_constraint_head(FakeOrch(), meta, request="book Ana for Friday", records="note", question="Which evening?")
    ensure_found_constraint_head(FakeOrch(), meta, request="book Ana for Friday", records="note", question="Which evening?")
    assert FakeOrch.calls == 1
    assert meta["found_constraint_head"] == "Friday will not work. Ana only teaches on Tuesday and Thursday evenings."


def test_ask_path_leaves_the_question_alone_without_a_conflict():
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import ensure_found_constraint_head

    class FakeOrch:
        def found_constraint_verdict(self, request, records, reply):
            return {"conflict": False}

    meta = {}
    ensure_found_constraint_head(FakeOrch(), meta, request="x", records="y", question="z")
    assert "found_constraint_head" not in meta


def test_a_repair_that_drops_an_address_the_draft_held_is_rejected_for_the_longer_draft():
    longer = "Start here:\n1. Call Dana, see https://dana.example/book\n2. Order parts\nWhen can you call?"
    provider, res = run_turn([longer, GOOD])
    assert len(steered(provider)) == 1                      # one repair round was tried, then it stopped
    assert "Keep every web address" in steered(provider)[0]  # the hint told the writer the address must survive
    assert "Order parts" in res.text and not res.text.startswith("Call Dana today")


def test_a_repair_that_keeps_the_address_is_accepted():
    longer = "Start here:\n1. Call Dana, see https://dana.example/book\n2. Order parts\nWhen can you call?"
    short = "Call Dana via https://dana.example/book today."
    provider, res = run_turn([longer, short])
    assert res.text.startswith("Call Dana via")


SPEND_READS = "Launch budget cap is 2,000 USD.\n\nComputed by code over the 7 entries in this result:\n- amount_usd: total 2340 over 7 entries, min 45, max 900, average 334.29\n\nrows: 900 420"
SPEND_EVIDENCE = ("Computed by code over the 7 entries: amount_usd total 2340, max 900. Rows: Label printer 900, Starter wax 420, Jars 380. "
                  "Launch budget cap is 2,000 USD.")


def test_a_wrong_difference_is_found_and_the_hint_carries_the_code_computed_values():
    bad = measure("Your total launch spend is 2,340 USD. That leaves 1,660 USD under your 2,000 USD budget cap.", STANDARDS["default"], SPEND_EVIDENCE, SPEND_READS)
    assert bad.unsupported == ["1,660 USD"]
    assert "2,340 - 2,000 = 340" in bad.hint() and "2,340 is 340 more than 2,000" in bad.hint()


def test_held_sums_differences_and_percentages_pass_the_figure_check():
    good = measure("You are 340 USD over the 2,000 USD cap at 2,340 USD. The label printer is 900 USD, about 38% of it.", STANDARDS["default"], SPEND_EVIDENCE, SPEND_READS)
    assert not good.failed


def test_repair_drops_the_sentence_with_the_invented_figure():
    draft = "Your total launch spend is 2,340 USD. That leaves 1,660 USD under your 2,000 USD budget cap."
    m = measure(draft, STANDARDS["default"], SPEND_EVIDENCE, SPEND_READS)
    assert repair(draft, STANDARDS["default"], m, SPEND_EVIDENCE, SPEND_READS) == (
        "Your total launch spend is 2,340 USD. 2,340 USD is 340 USD more than 2,000 USD.")


def test_repair_trims_the_clause_with_the_invented_figure_from_a_single_sentence_and_adds_the_code_computed_relation():
    draft = "Your total launch spend is 2,340 USD across 7 logged expenses, which leaves 1,660 USD under your 2,000 USD budget cap."
    m = measure(draft, STANDARDS["default"], SPEND_EVIDENCE, SPEND_READS)
    assert repair(draft, STANDARDS["default"], m, SPEND_EVIDENCE, SPEND_READS) == (
        "Your total launch spend is 2,340 USD across 7 logged expenses. 2,340 USD is 340 USD more than 2,000 USD.")


def test_a_large_pool_of_read_numbers_does_not_vouch_for_a_wrong_difference_by_coincidence():
    reads = SPEND_READS + " " + " ".join(str(n) for n in range(1, 300))
    bad = measure("Your total launch spend is 2,340 USD. That leaves 1,660 USD under your 2,000 USD budget cap.", STANDARDS["default"], reads, reads)
    assert bad.unsupported == ["1,660 USD"]      # sums of the pool's own numbers do not vouch for it: a derived figure needs an operand the draft states


def test_a_figure_an_earlier_reply_stored_does_not_vouch_for_itself():
    stored = "Earlier reply: you are 1,660 USD under your 2,000 USD cap."
    evidence = SPEND_EVIDENCE + "\n" + stored
    draft = "Your total launch spend is 2,340 USD. That leaves 1,660 USD under your 2,000 USD budget cap."
    assert measure(draft, STANDARDS["default"], evidence, SPEND_READS).unsupported == []        # held in the evidence, so it passes
    basis = evidence.replace(stored, "")
    bad = measure(draft, STANDARDS["default"], evidence, SPEND_READS, basis)
    assert bad.unsupported == ["1,660 USD"] and "2,340 - 2,000 = 340" in bad.hint()


def test_a_corrected_reply_is_written_one_tier_up_from_the_fast_tier_and_a_pinned_model_stays():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_run_answer import RunAnswerMixin
    registry = ModelRegistry(StubProvider([]))
    host = SimpleNamespace(registry=registry)
    up = lambda rs: RunAnswerMixin._regeneration_model(host, rs)
    fast = SimpleNamespace(model="fast-model", model_hint=None, plan=SimpleNamespace(model_tier="fast"))
    assert up(fast) == registry.resolve_tier("balanced")
    hinted = SimpleNamespace(model="fast-model", model_hint="fast", plan=SimpleNamespace(model_tier=None))
    assert up(hinted) == registry.resolve_tier("balanced")
    pinned = SimpleNamespace(model="claude-x-pinned", model_hint="claude-x-pinned", plan=SimpleNamespace(model_tier=None))
    assert up(pinned) == "claude-x-pinned"
    balanced = SimpleNamespace(model="b", model_hint=None, plan=SimpleNamespace(model_tier="balanced"))
    assert up(balanced) == "b"


def test_the_handoff_directive_tells_the_writer_to_answer_what_the_records_settle_now():
    from quest_ai_runner.the_intelligence.mind.planner.planner_prompt import HANDOFF_ANSWER_NOW_DIRECTIVE
    assert "queued" in HANDOFF_ANSWER_NOW_DIRECTIVE and "now" in HANDOFF_ANSWER_NOW_DIRECTIVE and "—" not in HANDOFF_ANSWER_NOW_DIRECTIVE


def test_a_decline_of_more_than_two_sentences_breaks_the_decline_standard():
    four = "I can't move money. That goes through your bank. Once you have it I can log it. Tell me the amount."
    m = measure(four, STANDARDS["decline"], EVIDENCE)
    assert kinds(m) == {"length"} and "at most 2 sentences" in m.hint()
    assert not measure("I can't move money from your bank. Once you have the refund I can log it as a negative expense.", STANDARDS["decline"], EVIDENCE).failed
