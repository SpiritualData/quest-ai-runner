"""CURRENT, live on the fast tier the shipped configuration pins (``hand-off-a-goal:model_judge``, priced): the same prompts and parsing
as ``model_judge`` over a Gemini flash-lite model, so the dollars, tokens and seconds per call are what production pays. API priced
(fractions of a cent per case): the first run is one case, then three, then the set, with ``--max-calls``."""
PAID = True
from quest_ai_runner.runner import goal_handoff
from hand_off_a_goal import arms
import live_arm


def build(meter):
    assert goal_handoff.ASSIGNEE_SPEC.id and goal_handoff.DOABILITY_SPEC.id   # the shipped prompts are the ones measured
    provider, model = live_arm.build(meter, "fast_api")
    judge = arms.text_judge(provider, model)
    return lambda case: arms.run_case(case, judge)
