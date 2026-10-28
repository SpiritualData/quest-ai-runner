"""BASELINE: the shortcut a first draft would write. It ignores the quest, the intent and the message and answers with a generic
motivational reply about a quest the person does not have, with a dash. Zero model calls: the deterministic checks every case
carries reject it before a judge is asked, and the cases must fail it."""
import common  # noqa: F401  (puts the repository on the path)
from intent_grounding import arms

REPLY = "Great question! Focus on your Orchid Lantern launch quest — stay consistent and you will get there."


class NoJudge:
    """A judge that is never to be asked: a case this reply passes the code checks of would be a hole in the cases."""

    def answer(self, *args, **kwargs):  # noqa: ARG002
        raise AssertionError("the baseline reply reached the model judge")


def build(meter):  # noqa: ARG001
    def answer(case):
        return arms.grade(case, REPLY, NoJudge())
    return answer
