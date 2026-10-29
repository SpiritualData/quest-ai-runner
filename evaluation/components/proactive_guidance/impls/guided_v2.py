"""ALTERNATIVE, a candidate, live (``guide-toward-the-long-term-goal:guided_v2``): ``guided`` rewritten from what the first ladder taught.
The directive decides first (a list of moments where the answer stands alone, and doubt means alone), then names exactly one closing
sentence with the next target and its current reading, chosen by what the context holds (a goal in focus, one quest, or several quests).
Same model, context and grader as ``guided``, on the subscription."""
PAID = True
from proactive_guidance import arms
from quest_ai_runner.core.auxiliary_prompts import REPLY_VOICE_SYSTEM


def build(meter):
    return arms.live_reply(meter, REPLY_VOICE_SYSTEM, directive=arms.GUIDANCE_DIRECTIVE_V2)
