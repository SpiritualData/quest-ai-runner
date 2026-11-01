"""ALTERNATIVE (``show-the-ais-work:plain_list``): the obvious first version, a flat list with one row per step in arrival order and every item
under it, whether or not the answer used it. Same events and strings as the Trail; judged exactly as the Trail is."""
PAID = True
import claude_judge
from work_trace_experience import arms

#: The model column of a row: the grader is the `claude-sonnet` family alias on the Claude Code subscription (the CLI picks the newest sonnet; the exact id is
#: not logged), and the real turns it judges were captured on Haiku (`claude-haiku-5-5`) by live_turns.py. An offline-only run calls no model.
JUDGE_AND_TURNS = "judge claude-sonnet alias; turns claude-haiku-5-5"


def build(meter):
    provider = claude_judge.cli_provider(meter, JUDGE_AND_TURNS)

    def experience(case):
        return dict(arm="plain_list", views=arms.views_for("plain_list", case), provider=provider)
    return experience
