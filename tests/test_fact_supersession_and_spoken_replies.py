"""Offline tests: two dated readings of one fact resolve to the latest in code; a draft stating an older one or two dates for one
thing breaks the standard; a figure the reads do not hold is caught on an information turn; a reply that is read aloud is held to the
spoken shape; a plain information turn is held to four sentences; the planner's `delivery` field reaches the plan."""
from datetime import date

from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import normalize_decision
from quest_ai_runner.the_intelligence.mind.response_writer.fact_supersession import resolve
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import (
    SHAPE_INFORMATION, SHAPE_SPOKEN, measure, repair, standard_for)

TODAY = date(2026, 11, 6)
EVIDENCE = """\
Onboarding completion: 40% of sign-ups finished onboarding (5 Oct)
Onboarding completion: 50.0% of sign-ups finished onboarding (5 Nov)
Paid subscribers: 0 (5 Nov)
The grant hold runs until 4 Jan.
"""


def test_two_readings_of_one_fact_resolve_to_the_latest_by_date():
    resolved = resolve(EVIDENCE, TODAY)
    assert len(resolved.current) == 1 and "50.0%" in resolved.current[0].line and resolved.current[0].stated == "5 Nov"
    assert "40" in resolved.stale_figures and (5, 10) in resolved.stale_days
    block = resolved.block()
    assert "50.0%" in block and "as of 5 Nov" in block and "40%" not in block      # the writer is told only the resolved fact


def test_a_draft_stating_the_older_reading_breaks_and_the_latest_passes():
    resolved = resolve(EVIDENCE, TODAY)
    std = standard_for(PlanDecision(action="answer", user_intent="ask"))
    bad = measure("Onboarding completion is 40% as of 5 Oct, and 50.0% as of 5 Nov.", std, EVIDENCE, resolved=resolved)
    assert any(v.kind == "superseded" for v in bad.violations)
    good = measure("Onboarding completion is 50.0% as of 5 Nov.", std, EVIDENCE, resolved=resolved)
    assert not good.failed


def test_readings_of_different_facts_are_not_merged():
    resolved = resolve("Paid subscribers: 12 (5 Nov)\nOnboarding completion: 50.0% (4 Nov)\n", TODAY)
    assert not resolved


def test_a_reply_giving_two_dates_for_one_thing_breaks():
    std = standard_for(PlanDecision(action="answer", user_intent="ask"))
    draft = "The three tester test was due 8 Nov. The three tester test is due 12 Nov."
    m = measure(draft, std, "The three tester test is due 8 Nov and due 12 Nov.", resolved=resolve("", TODAY))
    assert any(v.kind == "contradiction" for v in m.violations)


def test_a_figure_the_reads_do_not_hold_is_caught_on_an_information_turn():
    std = standard_for(PlanDecision(action="answer", user_intent="ask"))
    assert std.shape == SHAPE_INFORMATION
    m = measure("Paid subscribers reached 87% on 5 Nov.", std, EVIDENCE, resolved=resolve(EVIDENCE, TODAY))
    assert m.unsupported and any(v.kind == "unsupported" for v in m.violations)


def test_an_information_turn_is_held_to_three_sentences_an_order_report_to_two_and_a_written_deliverable_is_not_held():
    four, three = "One. Two. Three. Four.", "One. Two. Three."
    ask = standard_for(PlanDecision(action="answer", user_intent="ask"))
    act = standard_for(PlanDecision(action="answer", user_intent="act"))
    assert measure(four, ask, "").failed and not measure(three, ask, "").failed
    assert measure(three, act, "").failed and not measure("One. Two.", act, "").failed
    draft = standard_for(PlanDecision(action="answer", user_intent="act", deep_target="draft_in_chat"))
    assert not measure("One. Two. Three. Four. Five.", draft, "").failed


def test_spoken_delivery_selects_the_speakable_shape_and_enforces_it():
    plan = PlanDecision(action="answer", user_intent="ask", delivery="spoken")
    std = standard_for(plan)
    assert std.shape == SHAPE_SPOKEN
    draft = "**We are at 50.0%.** The \"onboarding\" test is stuck. Paid subscribers are 0. Funding is on hold. Ask me more?"
    m = measure(draft, std, "50.0% 0 onboarding test")
    kinds = {v.kind for v in m.violations}
    assert {"length", "format", "questions"} <= kinds
    fixed = repair(draft, std, m, "50.0% 0 onboarding test")
    assert "*" not in fixed and '"' not in fixed
    assert not measure("We are at 50.0% as of 5 Nov. Paid subscribers are still zero.", std, "50.0% 5 Nov zero").failed


def test_spoken_does_not_override_a_decline_or_a_question_back():
    assert standard_for(PlanDecision(action="answer", delivery="spoken", reply_shape="decline")).shape == "decline"
    assert standard_for(PlanDecision(action="answer", delivery="spoken", asks_first=True)).shape == "ask_first"


def test_the_planners_delivery_field_reaches_the_plan_and_a_bad_value_is_not_given():
    plan = normalize_decision({"action": "answer", "rationale": "r", "delivery": "spoken"}, OrchestratorConfig())
    assert plan.delivery == "spoken"
    assert normalize_decision({"action": "answer", "rationale": "r", "delivery": "shouted"}, OrchestratorConfig()).delivery is None


def test_the_writer_is_handed_the_resolved_fact_and_a_spoken_reply_directive():
    from hubofhubs.api import Mode
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator
    from .conftest import StubProvider, StubRetrieval

    class Capturing(StubProvider):
        prompts = []

        def answer(self, messages, *, model=None, system=None):
            self.prompts.append("\n".join(str(m.get("content")) for m in messages) + (system or ""))
            return "We are at 50.0% as of 5 Nov."

    provider = Capturing(decisions=[{"action": "read", "reads": [{"rel_path": "notes.md"}], "rationale": "look", "user_intent": "ask"},
                                    {"action": "answer", "rationale": "have it", "user_intent": "ask", "delivery": "spoken"}])
    notes = "Onboarding completion: 40% of sign-ups finished onboarding (5 Oct)\nOnboarding completion: 50.0% of sign-ups finished onboarding (5 Nov)\n"
    orch = Orchestrator(retrieval=StubRetrieval({"notes.md": notes}), provider=provider, registry=ModelRegistry(provider))
    orch.run("im on a call, where are we? keep it short, i will read it out")
    sent = "\n".join(provider.prompts)
    assert "FACTS WITH MORE THAN ONE DATED READING" in sent and "READ ALOUD" in sent


def test_the_read_budget_wrap_up_is_held_to_the_standard_and_regenerated_with_the_hint():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorConfig
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_run_answer import RunAnswerMixin

    steers = []
    rs = SimpleNamespace(plan=PlanDecision(action="read"), turn_user_intent="ask", turn_delivery="spoken", exec_record=None,
                         user_message="read it out", context_view=EVIDENCE, transcript="", recent_context_text="",
                         )
    host = SimpleNamespace(cfg=OrchestratorConfig(), link_evidence_for=lambda *a, **k: EVIDENCE,
                           resolved_facts_of=lambda rs, gathered: resolve(EVIDENCE, TODAY))

    def write(steer=None):
        steers.append(steer)
        return "Onboarding completion is 50.0% as of 5 Nov."

    long = "**Onboarding** is 40% as of 5 Oct. Paid subscribers: 0. Grants are on hold. Ask more? Done."
    out = RunAnswerMixin.hold_to_standard(host, rs, long, "", write)
    assert out == "Onboarding completion is 50.0% as of 5 Nov." and len(steers) == 1 and "YOUR PREVIOUS ANSWER" in steers[0]


def test_a_turn_that_ends_in_the_read_budget_wrap_up_is_regenerated_to_the_spoken_standard():
    """The whole path: the planner keeps reading until the budget is spent, the wrap-up writes a long marked-up draft, and the standard
    (selected from the planner's reported delivery) regenerates it once with the measurement's hint."""
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator
    from .conftest import StubProvider, StubRetrieval

    drafts = ["**Status:** We are at 50.0% as of 5 Nov. Paid subscribers are 0. Grants are on hold. Ask me more? Done.",
              "We are at 50.0% as of 5 Nov. Paid subscribers are still zero."]

    class Writer(StubProvider):
        steered = 0

        def answer(self, messages, *, model=None, system=None):
            text = "\n".join(str(m.get("content")) for m in messages)
            if "YOUR PREVIOUS ANSWER" in text:
                Writer.steered += 1
            return drafts[1] if Writer.steered else drafts[0]

    reads = [{"action": "read", "reads": [{"rel_path": f"n{i}.md"}], "rationale": "look", "user_intent": "ask", "delivery": "spoken"} for i in range(12)]
    files = {f"n{i}.md": "Onboarding completion 50.0% on 5 Nov. Paid subscribers 0 on 5 Nov. Grants on hold." for i in range(12)}
    provider = Writer(decisions=reads)
    orch = Orchestrator(retrieval=StubRetrieval(files), provider=provider, registry=ModelRegistry(provider),
                        config=OrchestratorConfig(max_steps=3, verify_claims=False, answer_goal_max_iterations=1, overseer=False))
    res = orch.run("im on a call, where are we? keep it short, i will read it out")
    assert Writer.steered >= 1 and "*" not in res.text and "?" not in res.text


def test_two_different_goals_each_with_its_own_due_day_are_not_one_thing_with_two_dates():
    # Measured 2026-11-11: a status listing two overdue goals was read as one thing with two dates, because "due", "days" and "overdue" were taken for the subject.
    std = standard_for(PlanDecision(action="answer", user_intent="ask"))
    draft = "Book the plumber, which was due 1 November and is 10 days overdue. The schedule check is due 8 November (3 days overdue)."
    m = measure(draft, std, "Book the plumber (due 2026-11-01). The schedule check (due 2026-11-08).", resolved=resolve("", TODAY))
    assert not any(v.kind == "contradiction" for v in m.violations)
