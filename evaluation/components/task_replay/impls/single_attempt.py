"""FLOOR, live: no iterate-by-spec. The doctrine arm adds nothing to the brief and reads no metric, the metric tool is not
offered (so no gate), and the goal gets a single attempt. Same worker, same done verifier, same scratch workspace."""
PAID = True
from task_replay import replay


def build(meter):
    meter.model = "claude-code-subscription"
    return lambda case: replay.replay(case, replay.FLOOR, meter)
