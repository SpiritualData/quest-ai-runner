"""ALTERNATIVE under test, and the arm in the app today (``show-the-ais-work:trail``): the Trail of quest-frontend, a live line, one summary line and expandable groups, from
``src/utils/workTrace.ts`` and its English strings, through the bridge. Judged on real turns by one Claude Code subscription call per case."""
PAID = True
import claude_judge
from work_trace_experience import arms


def build(meter):
    provider = claude_judge.cli_provider(meter, "claude-code-subscription")

    def experience(case):
        return dict(arm="trail", views=arms.views_for("trail", case), provider=provider)
    return experience
