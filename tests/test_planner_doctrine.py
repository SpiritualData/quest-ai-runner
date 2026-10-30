"""The situational planner profile: the doctrine as rules, picked per turn by structured facts, never by words.

Pinned here:

  * the full profile is every rule in order: assembling the table with no situation IS ``PLANNER_PROMPT``, so the default
    behaviour of a lane is the text it always had, and the compact profile is unchanged;
  * the table is well formed (unique ids, conditions name only declared facts) and its wording is the model-visible text of
    the sections, moved not rewritten;
  * the situation is built from structured state (mode flags, the reach verdict, the planner's own ``user_intent`` verdict,
    a consumer count), a fact the turn does not know leaves the rule IN, and the message's own words change nothing;
  * through the real ``_plan``: a hold-off or brainstorm turn drops the actions it suspends, a question after the first
    step drops the change-authoring rules, and the default profile never changes.

Fully offline, no LLM calls.
"""
from hubofhubs.doctrine import assemble_doctrine, check_rules

from quest_ai_runner.core.planner_doctrine import FACTS, SITUATIONAL_PROFILE, planner_situation, situational_template
from quest_ai_runner.core.planner_prompt import (
    PLANNER_DECISION_RUBRIC,
    PLANNER_PROFILES,
    PLANNER_PROMPT,
    PLANNER_RULES,
)

from .test_planner_prompt_profiles import CapturingProvider, build

BY_ID = {rule.id: rule for rule in PLANNER_RULES}
DEEP_MARK = '  - "deep": this needs REAL WORK.'
CHANGE_MARK = "CODE / FILE CHANGE COMMANDS"
PROGRESS_MARK = "CHECKING ON WORK ALREADY IN PROGRESS"


def test_the_full_profile_is_every_rule_in_order():
    assert assemble_doctrine(PLANNER_RULES, {}).text == PLANNER_PROMPT == "".join(r.text for r in PLANNER_RULES)
    assert PLANNER_PROFILES["full"] is PLANNER_PROMPT
    assert SITUATIONAL_PROFILE not in PLANNER_PROFILES   # per-turn, so not a static template


def test_the_rule_table_is_well_formed_and_keeps_the_rubric_whole():
    assert check_rules(PLANNER_RULES, FACTS) == []
    assert BY_ID["decision-order"].text == PLANNER_DECISION_RUBRIC + "\n" and not BY_ID["decision-order"].applies_when
    assert not BY_ID["role"].applies_when and not BY_ID["answer-action"].applies_when


def test_unknown_facts_keep_every_rule_and_a_known_reason_drops_some():
    everything = situational_template(planner_situation())
    assert everything.excluded == () and everything.text == PLANNER_PROMPT
    quiet = situational_template(planner_situation(brainstorm=True))
    assert {"deep-action", "confirm-action", "command-vs-question"} <= set(quiet.excluded)
    assert quiet.tokens < everything.tokens


def test_the_situation_is_built_from_structured_state_only():
    assert planner_situation(brainstorm=True).mode == "brainstorm"
    assert planner_situation(brainstorm=True, user_vetoed_task=True).mode == "hold_off"
    assert planner_situation(user_vetoed_task=True).mode == "normal"     # the veto only rides the no-action latch
    assert planner_situation(turn_facts={"user_intent": "act"}).asks_change is True
    for intent in ("ask", "inform", "hold_off"):
        assert planner_situation(turn_facts={"user_intent": intent}).asks_change is False
    assert planner_situation(turn_facts={"user_intent": None}).asks_change is None
    assert planner_situation(turn_facts={"runs_in_flight": 0}).run_in_progress is False
    assert planner_situation(turn_facts={"runs_in_flight": 2}).run_in_progress is True
    assert planner_situation(turn_facts={"runs_in_flight": None}).run_in_progress is None
    assert planner_situation(turn_facts={"reach": "outside", "bogus": 1}).reach == "outside"


def test_default_profile_is_untouched_and_never_reads_the_situational_path():
    provider = CapturingProvider()
    build(provider)._plan("tell me about my plan", "", "ctx", [], brainstorm=True)
    assert DEEP_MARK in provider.prompts[0] and CHANGE_MARK in provider.prompts[0]


def situational_prompt(message="tell me about my plan", **plan_kwargs):
    provider = CapturingProvider()
    build(provider, planner_prompt_profile=SITUATIONAL_PROFILE)._plan(message, "", "ctx", [], **plan_kwargs)
    return provider.prompts[0]


def test_a_cold_first_step_sends_every_rule_whatever_the_message_says():
    for message in ("fix the login bug and delete the old module", "thanks"):
        prompt = situational_prompt(message)
        assert DEEP_MARK in prompt and CHANGE_MARK in prompt and PROGRESS_MARK in prompt


def test_brainstorm_and_hold_off_turns_drop_the_actions_they_suspend():
    for flags in ({"brainstorm": True}, {"brainstorm": True, "user_vetoed_task": True}):
        prompt = situational_prompt(**flags)
        assert DEEP_MARK not in prompt and CHANGE_MARK not in prompt and '  - "confirm": reserved' not in prompt
        assert '  - "answer": you have ENOUGH' in prompt and "DECIDE IN THIS ORDER" in prompt


def test_a_question_after_the_first_step_drops_the_change_authoring_rules_and_keeps_the_gate():
    prompt = situational_prompt(step=1, turn_facts={"user_intent": "ask", "runs_in_flight": 0})
    assert DEEP_MARK not in prompt and CHANGE_MARK not in prompt and PROGRESS_MARK not in prompt
    assert "QUESTION vs STATEMENT vs COMMAND" in prompt   # the call between answering and acting stays
    acting = situational_prompt(step=1, turn_facts={"user_intent": "act"})
    assert DEEP_MARK in acting and CHANGE_MARK in acting


def test_the_reach_verdict_picks_the_hand_off_doctrine():
    provider = CapturingProvider()
    orch = build(provider, planner_prompt_profile=SITUATIONAL_PROFILE)
    mark = "REACH OF A READ: reads reach ONLY"
    for verdict, expected in (("outside", True), ("inside", False), ("world", False), (None, True)):
        orch.reach_verdict = lambda message, v=verdict: {"reach": v} if v else None
        orch._plan("do the thing", "", "ctx", [])
        assert (mark in provider.prompts[-1]) is expected, verdict
