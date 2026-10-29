"""PRIOR, live (``guide-toward-the-long-term-goal:reply_voice``): the repository's reply voice as it was before the long-term guidance section (the shipped prompt with ``LONG_TERM_GUIDANCE`` taken out) (answer first, then be proactive
only where it is worth the person's time) is handed the labelled context and the message, on the subscription (``claude -p``, haiku).
Model calls are counted (a reply and a grader per case, the grader skipped when a check by code already failed). Stay on the ladder of
esoi/metric-run-policy.md: 3 cases, then 10, then the set, and always pass ``--max-calls``."""
PAID = True
from proactive_guidance import arms
from quest_ai_runner.core.auxiliary_prompts import LONG_TERM_GUIDANCE, REPLY_VOICE_SYSTEM


def build(meter):
    return arms.live_reply(meter, REPLY_VOICE_SYSTEM.replace(LONG_TERM_GUIDANCE, ""))
