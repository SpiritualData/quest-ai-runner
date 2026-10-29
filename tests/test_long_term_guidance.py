"""The reply voice carries the measured long-term guidance section (esoi solution guide-toward-the-long-term-goal, implementation
guided_v2): it decides first when to say nothing about the goal, then adds one closing sentence with the next target and its reading."""
from quest_ai_runner.core.auxiliary_prompts import LONG_TERM_GUIDANCE, REPLY_VOICE_SYSTEM


def test_the_reply_voice_carries_the_guidance_once():
    assert REPLY_VOICE_SYSTEM.count(LONG_TERM_GUIDANCE) == 1


def test_the_guidance_decides_first_and_asks_for_one_grounded_sentence():
    text = LONG_TERM_GUIDANCE
    for must in ("Step 1, decide", "urgent", "upset", "exactly one sentence", "current reading", "Never invent a date or figure",
                 "Never put the sentence before the answer", "When in doubt, write the answer alone"):
        assert must in text
    assert "—" not in text and "–" not in text
