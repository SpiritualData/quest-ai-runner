"""ALTERNATIVE, a candidate, live (``guide-toward-the-long-term-goal:guided``): the reply voice plus one directive that points the person at the next
milestone and the current reading at the level that fits the moment, and says when to leave it out. The directive lives in this
component (``arms.GUIDANCE_DIRECTIVE``) until a measured win moves it into the reply voice. Same model, context and grader as
``reply_voice``, on the subscription."""
PAID = True
from proactive_guidance import arms
from quest_ai_runner.core.auxiliary_prompts import LONG_TERM_GUIDANCE, REPLY_VOICE_SYSTEM


def build(meter):
    return arms.live_reply(meter, REPLY_VOICE_SYSTEM.replace(LONG_TERM_GUIDANCE, ""), directive=arms.GUIDANCE_DIRECTIVE)
