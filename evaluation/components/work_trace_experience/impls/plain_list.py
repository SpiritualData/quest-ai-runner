"""ALTERNATIVE (``show-the-ais-work:plain_list``): the obvious first version, a flat list with one row per step in arrival order and every item
under it, whether or not the answer used it. Same events and strings as the Trail; judged exactly as the Trail is."""
PAID = True
import claude_judge
from work_trace_experience import arms


def build(meter):
    provider = claude_judge.cli_provider(meter, "claude-code-subscription")

    def experience(case):
        return dict(arm="plain_list", views=arms.views_for("plain_list", case), provider=provider)
    return experience
