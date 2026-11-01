"""ALTERNATIVE under test, and the arm in the app today (``show-the-ais-work:trail``): the Trail of quest-frontend, a live line, one summary line and expandable groups, from
``src/utils/workTrace.ts`` and its English strings, through the bridge. Judged on real turns by one Claude Code subscription call per case."""
PAID = True
import claude_judge
from work_trace_experience import arms

#: The model column of a row: the grader is the `claude-sonnet` family alias on the Claude Code subscription (the CLI picks the newest sonnet; the exact id is
#: not logged), and the real turns it judges were captured on Haiku (`claude-haiku-5-5`) by live_turns.py. An offline-only run calls no model.
JUDGE_AND_TURNS = "judge claude-sonnet alias; turns claude-haiku-5-5"


def build(meter):
    provider = claude_judge.cli_provider(meter, JUDGE_AND_TURNS)

    def experience(case):
        return dict(arm="trail", views=arms.views_for("trail", case), provider=provider)
    return experience
