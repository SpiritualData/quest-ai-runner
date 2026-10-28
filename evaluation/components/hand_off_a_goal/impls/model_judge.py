"""CURRENT, live on the Claude Code subscription (``hand-off-a-goal:model_judge``): the shipped prompts and parsing with a small model
behind them, `claude -p` on its cheapest tier. No API dollars; model calls are counted. PAID in the sense of the run policy (it spends
subscription calls): stay on the ladder and always pass ``--max-calls``."""
PAID = True
from quest_ai_runner.runner import goal_handoff
from hand_off_a_goal import arms
import live_arm


def build(meter):
    assert goal_handoff.ASSIGNEE_SPEC.id and goal_handoff.DOABILITY_SPEC.id   # the shipped prompts are the ones measured
    provider, model = live_arm.build(meter, "subscription")
    judge = arms.text_judge(provider, model)
    return lambda case: arms.run_case(case, judge)
