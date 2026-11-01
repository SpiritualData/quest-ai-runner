"""BASELINE and incumbent (``show-the-ais-work:no_trace``): the app shows nothing of the AI's work, as before the Trail. Zero model calls. The emitted events still go
through the truthfulness gate, so what is shown is the only difference between the arms."""
import claude_judge
from work_trace_experience import arms


def build(meter):
    provider = claude_judge.cli_provider(meter, "claude-code-subscription")

    def experience(case):
        return dict(arm="no_trace", views=arms.views_for("no_trace", case), provider=provider)
    return experience
