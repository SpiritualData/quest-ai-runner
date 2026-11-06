"""BUILT, not selected (``critical-thinking:every_request_measured``): a chat turn or in-process work passes the same begin and finish as a deep
goal (``TurnMetrics`` over ``GoalMetrics``): the metric is read before the work and again when the reply is built, rows go to an in-memory
ledger. It proposes no metric and answers no regression (those are ``measured_proposed_and_answered``). Zero model calls."""
import common  # noqa: F401
from quest_ai_runner.the_intelligence.critical_thinking.area import hold_case


def build(meter):  # noqa: ARG001
    return lambda case: hold_case(case, every_request=True)
