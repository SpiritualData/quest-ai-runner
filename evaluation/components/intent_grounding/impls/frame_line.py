"""CURRENT-WITH-INTENT, live (``ground-in-intent:frame-line``): the reply model is handed the computed intent frame under the
repository's own ``INTENT_FRAME_PREAMBLE`` (the block the autopilot brief carries), then the person's message. The reply is on
the subscription's cheapest tier through ``claude -p`` and the judge is chat_quality's, so API dollars are zero; the model calls
are counted (a reply and, unless a code check already failed, a judge call per case). Stay on the ladder of
esoi/metric-run-policy.md: one case, three, ten, then the set, and always pass ``--max-calls``."""
PAID = True
from intent_grounding import arms
from quest_ai_runner.runner.autopilot_briefs import INTENT_FRAME_PREAMBLE


def build(meter):
    return arms.live_arm(meter, with_intent=True, preamble=INTENT_FRAME_PREAMBLE)
