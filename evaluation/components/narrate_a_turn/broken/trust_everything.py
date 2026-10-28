"""BASELINE: the shortcut a first draft would write. One upbeat line for every turn that claims progress nobody made (a count and a
dash), with no look at the message. Zero model calls: the deterministic checks of the scorer reject it before a grader is asked."""
from narrate_a_turn import score

LINE = "Great news — I checked your quest and 3 of 4 goals are already on track."


def build(meter):  # noqa: ARG001
    class NoGrader:
        def answer(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("the baseline beat reached the grader")

    def narrate(case):
        return score.judged(case, {"beat": LINE, "seconds": 0.0}, NoGrader())
    return narrate
