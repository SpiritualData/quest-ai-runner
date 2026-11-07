"""Offline tests: first-person words copied from a record, a general question answered from the question alone with no context assembled,
unrelated figures, the re-plan cap, the process cache of assemblies and the rep profile read in parallel with the assembly."""
import time
from typing import Any, Dict, List, Optional

from hubofhubs.api import AssembledContext, Mode

from .conftest import StubProvider, StubRetrieval
from .test_answer_explanation import RecordingSink
from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import parse_goal_condition_reply, take_needs_records
from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import (
    copied_first_person, measure, repair, standard_for, unrelated_sentences)
from quest_ai_runner.the_intelligence.subconscious.context_retriever.cached_context_assembler import CachedContextAssembler
from quest_ai_runner.the_intelligence.subconscious.context_retriever.rep_context_assembler import RepContextAssembler
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

NOTE = "Two email campaigns are ready and awaiting my preview before they go out."


def test_first_person_words_copied_from_a_record_are_found_and_the_assistants_own_are_not():
    assert copied_first_person("Two campaigns are waiting for my preview before they go out.", [NOTE]) == ["my preview before"]
    assert copied_first_person("I could not re-check it after 5 Nov.", [NOTE]) == []
    assert copied_first_person("Two campaigns await Sam's preview.", [NOTE]) == []


def test_the_voice_check_runs_on_information_and_spoken_shapes_and_not_elsewhere():
    draft = "Two campaigns are waiting for my preview before they go out."
    for plan in (PlanDecision(action="answer", user_intent="ask"), PlanDecision(action="answer", user_intent="ask", delivery="spoken")):
        m = measure(draft, standard_for(plan), NOTE)
        assert any(v.kind == "voice" for v in m.violations)
    assert not any(v.kind == "voice" for v in measure(draft, standard_for(PlanDecision(action="answer")), NOTE).violations)


def test_the_request_analyser_reports_needs_records_false_and_it_leaves_the_filters_alone():
    goal, constraints = parse_goal_condition_reply('What is a quest?\n{"needs_records": false}')
    assert goal == "What is a quest?" and constraints == {"needs_records": False}
    assert take_needs_records(constraints) == (False, None)
    _, kept = parse_goal_condition_reply('Show last week\n{"needs_records": false, "topic_terms": ["goals"]}')
    assert take_needs_records(kept) == (False, {"topic_terms": ["goals"]})
    assert take_needs_records({"topic_terms": ["x"]}) == (None, {"topic_terms": ["x"]})
    _, true_kept = parse_goal_condition_reply('x\n{"needs_records": true, "topic_terms": ["a"]}')
    assert take_needs_records(true_kept)[0] is None


class GeneralProvider(StubProvider):
    def __init__(self):
        super().__init__(decisions=[{"action": "read", "reads": [{"rel_path": "n.md"}], "rationale": "must not run"}])
        self.writer_prompts: List[str] = []
        self.planner_calls = 0

    def plan(self, prompt, *, model, tool_schema):
        self.planner_calls += 1
        return super().plan(prompt, model=model, tool_schema=tool_schema)

    def answer(self, messages, *, model=None, system=None):
        text = "\n".join(str(m.get("content")) for m in messages) + (system or "")
        if "request analyser" in text:
            return 'What a quest is.\n{"needs_records": false}'
        self.writer_prompts.append(text)
        return "A quest is a goal you pursue with milestones."


class CountingAssembler:
    def __init__(self):
        self.calls = 0

    def assemble(self, task_text, *, meta=None):
        self.calls += 1
        return AssembledContext(context_view="GOALS: you have two overdue goals")


def test_a_general_question_assembles_no_context_runs_no_planner_and_is_written_from_the_question_alone():
    provider = GeneralProvider()
    assembler = CountingAssembler()
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), context_assembler=assembler,
                        config=OrchestratorConfig(overseer=True))
    res = orch.run("what exactly is a quest?", mode=Mode.LIVE, sink=RecordingSink())
    assert res.kind == "answer" and "milestones" in res.text
    assert assembler.calls == 0 and provider.planner_calls == 0
    assert provider.writer_prompts and "overdue goals" not in "\n".join(provider.writer_prompts)


def test_the_other_arm_still_assembles_context_for_the_same_question():
    provider = GeneralProvider()
    assembler = CountingAssembler()
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), context_assembler=assembler,
                        config=OrchestratorConfig(cheap_information_turns=False))
    orch.run("what exactly is a quest?", mode=Mode.LIVE, sink=RecordingSink())
    assert assembler.calls >= 1


def test_a_sentence_with_a_figure_that_the_question_and_the_facts_do_not_touch_is_unrelated():
    facts = ["onboarding completion 50.0% on 5 Nov"]
    draft = "Onboarding completion is 50.0% as of 5 Nov. Your grant hold runs until 4 Jan."
    assert unrelated_sentences(draft, "whats the latest on onboarding?", facts) == ["Your grant hold runs until 4 Jan."]
    assert unrelated_sentences(draft, "whats the latest on onboarding?", []) == []       # no reported facts, no basis, nothing flagged
    std = standard_for(PlanDecision(action="answer", user_intent="ask"))
    m = measure(draft, std, draft, facts=facts, question="whats the latest on onboarding?")
    assert any(v.kind == "unrelated" for v in m.violations)
    assert "grant hold" not in repair(draft, std, m, draft)


def test_an_information_turn_re_plans_once_then_answers_from_what_it_read():
    reads = [{"action": "read", "reads": [{"rel_path": f"n{i}.md"}], "rationale": "look", "user_intent": "ask"} for i in range(6)]
    files = {f"n{i}.md": f"fact {i}" for i in range(6)}
    seen = {}
    for cheap in (True, False):
        provider = StubProvider(decisions=reads + [{"action": "answer", "rationale": "have it", "user_intent": "ask"}])
        o = Orchestrator(retrieval=StubRetrieval(files), provider=provider, registry=ModelRegistry(provider),
                         config=OrchestratorConfig(cheap_information_turns=cheap, max_steps=8, overseer=False,
                                                   verify_claims=False, answer_goal_max_iterations=1))
        res = o.run("what do the notes say about facts", mode=Mode.LIVE, sink=RecordingSink())
        seen[cheap] = res.steps
    assert seen[True] <= 3 < seen[False]


def test_the_process_cache_serves_an_identical_request_and_forgets_on_a_record():
    clock = [0.0]
    inner = CountingAssembler()
    cached = CachedContextAssembler(inner, ttl=60, clock=lambda: clock[0])
    meta = {"scope_tags": ["q1"], "assembly_deadline": 1.0}
    cached.assemble("same question", meta=meta)
    cached.assemble("same question", meta={"scope_tags": ["q1"], "assembly_deadline": 99.0})
    assert inner.calls == 1                                          # the deadline is not part of the key
    cached.assemble("same question", meta={"scope_tags": ["q2"]})
    assert inner.calls == 2                                          # another scope is another request
    clock[0] = 61.0
    cached.assemble("same question", meta=meta)
    assert inner.calls == 3                                          # past the window
    inner.record = lambda *a, **k: None
    cached.record("same question", {})
    cached.assemble("same question", meta=meta)
    assert inner.calls == 4                                          # a record clears the memory


def test_the_rep_profile_is_read_while_the_inner_assembler_works():
    class Slow:
        def assemble(self, text, *, meta=None):
            time.sleep(0.3)
            return AssembledContext(context_view="inner")

    class Client:
        def get_ai_profile(self, user_id, team_id=""):
            time.sleep(0.3)
            return {"context_prefs": {}}

    rep = RepContextAssembler(Slow(), client=Client())
    rep._rep_block = lambda stashed=None: (time.sleep(0.3) or "REP")
    started = time.monotonic()
    out = rep.assemble("q")
    assert time.monotonic() - started < 0.55 and "REP" in out.context_view and "inner" in out.context_view


def test_an_information_or_spoken_reply_is_rewritten_once_and_the_other_arm_keeps_the_configured_rounds():
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import repair_limit
    info = standard_for(PlanDecision(action="answer", user_intent="ask"))
    one_step = standard_for(PlanDecision(action="answer", reply_shape="one_step"))
    assert repair_limit(OrchestratorConfig(), info) == 1 and repair_limit(OrchestratorConfig(), one_step) == 2
    assert repair_limit(OrchestratorConfig(cheap_information_turns=False), info) == 2


def test_a_question_is_never_routed_to_a_deep_run_and_an_order_report_is_held_to_two_sentences():
    from quest_ai_runner.the_world.tools import tool_guards
    from quest_ai_runner.the_intelligence.mind.response_writer.reply_standards import SHAPE_SETTLED
    from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import take_information_only
    assert tool_guards.escalation_route(PlanDecision(action="deep"), "ask", None, False, False) == tool_guards.ROUTE_INLINE
    assert tool_guards.escalation_route(PlanDecision(action="deep"), None, None, False, False) == tool_guards.ROUTE_DEEP
    assert standard_for(PlanDecision(action="answer", user_intent="act")).shape == SHAPE_SETTLED
    assert standard_for(PlanDecision(action="answer", user_intent="act", deep_target="draft_in_chat")).shape != SHAPE_SETTLED
    three = "It is set. It repeats weekly. The next one is Monday."
    assert measure(three, standard_for(PlanDecision(action="answer", user_intent="act")), three).failed
    _, constraints = parse_goal_condition_reply('Tell the count.\n{"information_only": true}')
    assert take_information_only(constraints) == (True, None)
    assert take_information_only({"topic_terms": ["x"]}) == (False, {"topic_terms": ["x"]})


class AskingProvider(GeneralProvider):
    def answer(self, messages, *, model=None, system=None):
        text = "\n".join(str(m.get("content")) for m in messages) + (system or "")
        if "request analyser" in text:
            return 'Tell the count.\n{"information_only": true}'
        return "Four."


class FullOnlyAssembler(CountingAssembler):
    def assemble(self, task_text, *, meta=None):
        from hubofhubs.api import retrieve_only
        self.calls += 1
        self.cheap = getattr(self, "cheap", 0) + (1 if retrieve_only(meta) else 0)
        return AssembledContext(context_view="GOALS: four", card_metadata=[{"relevance_score": 0.9}])


def test_a_question_that_only_asks_to_be_told_is_assembled_on_the_cheap_path_first():
    provider = AskingProvider()
    provider.decisions = [{"action": "answer", "rationale": "have it", "user_intent": "ask"}]
    assembler = FullOnlyAssembler()
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), context_assembler=assembler,
                        config=OrchestratorConfig(overseer=True))
    orch.run("how many goals are on the big quest?", mode=Mode.LIVE, sink=RecordingSink())
    assert assembler.calls >= 1 and assembler.cheap >= 1


def test_a_tool_catalog_search_is_not_evidence_for_the_information_turn_cap():
    from quest_ai_runner.the_intelligence.mind.context_intake.planner_context import answer_evidence
    menu = {"kind": "query", "locator": "tools(read goals)", "text": "TOOLS MATCHING YOUR SEARCH"}
    ops = {"kind": "query", "locator": "list_operations", "text": "menu", "discovery": True}
    read = {"kind": "query", "locator": "tool:list_quest_goals", "text": "595 matching goals"}
    assert answer_evidence([menu, ops]) == [] and answer_evidence([menu, ops, read]) == [read]


def test_a_version_that_names_a_persons_existing_goal_is_reported_under_that_goals_title_and_the_rest_are_created():
    from .test_milestone_cue import A_EXISTING, B_NEW, alt_answer, alt_registry
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
    rows = [{"id": "p1", "title": "Push post A", "text": "a person's goal", "spec_solution_id": None, "completed": False},
            {"id": "x1", "title": "Budget decision recorded", "text": "other", "spec_solution_id": None, "completed": False}]
    calls = []
    named = [dict(A_EXISTING, title="Push post A", goal_id=""), dict(B_NEW, title="Push post B", goal_id="")]
    tail = alternatives_flow.run(alt_registry(calls, rows), "q1", "write a couple of posts for the push", "reply", lambda prompt: alt_answer(*named))
    assert "- Push post A (already on the quest, left exactly as it is)" in tail and "Budget decision" not in tail
    assert all(c.get("title") != "Push post A" for c in calls)
