"""Situation standards at the answer checkpoint: the measurements, the code-built repair rounds and the last-resort repair."""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.orchestrator_decide_tool import DECIDE_TOOL
from quest_ai_runner.core.orchestrator_decisions import normalize_decision
from quest_ai_runner.core.overseer import OVERSEER_PROMPT
from quest_ai_runner.core.planner_prompt import PLANNER_PROFILES
from quest_ai_runner.core.reply_standards import REPLY_SHAPES, STANDARDS, measure, repair, standard_for

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
    from quest_ai_runner.core.reply_standards import STANDARDS, SHAPE_DEFAULT, day_count_errors, measure
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
    from quest_ai_runner.core.reply_standards import event_offset_errors
    wrong = ("If they arrive a week early they would come on 13 Nov, which leaves you until 10 Nov, and your own "
             "target is already 3 days before arrival (17 Nov). That squeezes the work.")
    errors = event_offset_errors(wrong)
    assert len(errors) == 1 and "10 Nov" in errors[0] and "17 Nov" in errors[0]
    right = "They would come on 13 Nov, so your target is 3 days before arrival (10 Nov)."
    assert event_offset_errors(right) == []
    assert event_offset_errors("The race is 2 days after arrival (22 Nov).") == []   # no earlier date to count from
