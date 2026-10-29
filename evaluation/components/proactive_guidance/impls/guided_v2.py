"""CURRENT, live (``guide-toward-the-long-term-goal:guided_v2``): the shipped ``REPLY_VOICE_SYSTEM``, whose ``LONG_TERM_GUIDANCE`` section
decides first (moments where the answer stands alone, and doubt means alone), then adds exactly one closing sentence with the next target
and its current reading, chosen by what the context holds (a goal in focus, one quest, or several quests). Same model, context and grader
as the other arms, on the subscription."""
PAID = True
from proactive_guidance import arms
from quest_ai_runner.core.auxiliary_prompts import REPLY_VOICE_SYSTEM


def build(meter):
    return arms.live_reply(meter, REPLY_VOICE_SYSTEM)
