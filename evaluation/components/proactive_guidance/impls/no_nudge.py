"""FLOOR (``guide-toward-the-long-term-goal:no_nudge``): the reply is the plain answer to the request and nothing about the long-term goal.
No reply model call (the only model calls are the grader's). It is perfect on the restraint class and fails every nudge case."""
from proactive_guidance import arms


def build(meter):
    return arms.scripted(meter, lambda case: case["plain_answer"])
