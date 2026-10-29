"""BASELINE: the shortcut a first draft would write. It leads every reply with the next milestone and the current reading, whatever the
request, then adds the plain answer. It hijacks every request, so the checks by code reject it on every case (a nudge before the answer, or a
nudge where none belongs) before a grader is asked, and the cases must fail it."""
import common  # noqa: F401  (puts the repository on the path)


class NoGrader:
    def answer(self, *args, **kwargs):  # noqa: ARG002
        raise AssertionError("the baseline reply reached the grader")


def build(meter):  # noqa: ARG001
    def reply(case):
        lead = f"Your next milestone is {case['milestone_keys'][0]} and you are at {case['metric_values'][0]}, so let's focus on that."
        return {"reply": lead + " " + case["plain_answer"], "provider": NoGrader()}
    return reply
