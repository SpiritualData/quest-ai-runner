"""The reply voice carries the measured long-term guidance section (esoi solution guide-toward-the-long-term-goal, implementation
guided_v2): it decides first when to say nothing about the goal, then adds one closing sentence with the next target and its reading."""
from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import LONG_TERM_GUIDANCE, REPLY_VOICE_SYSTEM


def test_the_reply_voice_carries_the_guidance_once():
    assert REPLY_VOICE_SYSTEM.count(LONG_TERM_GUIDANCE) == 1


def test_the_guidance_decides_first_and_asks_for_one_grounded_sentence():
    text = LONG_TERM_GUIDANCE
    for must in ("Step 1, decide", "urgent", "upset", "exactly one sentence", "current reading", "Never invent a date or figure",
                 "Never put the sentence before the answer", "When in doubt, write the answer alone"):
        assert must in text
    assert "—" not in text and "–" not in text


def test_long_term_guidance_lets_a_stated_length_win_over_the_closing_sentence():
    from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import LONG_TERM_GUIDANCE
    assert "in one breath" in LONG_TERM_GUIDANCE and "that length, or the question alone, wins" in LONG_TERM_GUIDANCE


def test_the_reply_voice_asks_first_when_the_next_step_needs_the_person():
    from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import REPLY_VOICE_SYSTEM
    assert "ASKING COMES FIRST" in REPLY_VOICE_SYSTEM and "under 500 characters" in REPLY_VOICE_SYSTEM
    assert "the question alone" in REPLY_VOICE_SYSTEM


def test_the_planner_rubric_asks_what_only_the_person_knows_instead_of_handing_off():
    from quest_ai_runner.the_intelligence.mind.planner.planner_prompt import PLANNER_DECISION_RUBRIC, PLANNER_PROMPT_COMPACT
    for text in (PLANNER_DECISION_RUBRIC, PLANNER_PROMPT_COMPACT):
        assert "ASK WHAT ONLY THEY KNOW" in text
    assert "never hand it to" in PLANNER_DECISION_RUBRIC


def test_the_closing_sentence_is_left_out_when_nothing_moved():
    assert "nothing moved" in LONG_TERM_GUIDANCE and "never ends with the sentence" in LONG_TERM_GUIDANCE


def test_a_confirmation_of_something_set_up_carries_no_closing_sentence():
    assert "asked you to set something up" in LONG_TERM_GUIDANCE and "one or two short sentences with nothing added" in LONG_TERM_GUIDANCE
    assert "—" not in LONG_TERM_GUIDANCE
