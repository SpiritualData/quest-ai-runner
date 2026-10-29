"""CURRENT, live: the real orchestrator the way production builds it (the claude CLI backend on the cheapest tier, the standard
planner, the real tool catalogue on the world of ``world.json``, the fixture web), and the real ``send`` command with a real
model. PAID in subscription calls: it spends Claude Code subscription time through ``claude -p``, no API dollars. Stay on the
staged ladder of esoi/metric-run-policy.md and always pass ``--max-calls``. ``QAR_EVAL_MODEL`` picks the planner tier (default
haiku)."""
PAID = True
import os

import common
import live_arm
from end_to_end import drive


def build(meter):
    tier = os.environ.get("QAR_EVAL_MODEL", "haiku")
    ack_provider, _model = live_arm.build(meter, "subscription")

    def turn(case):
        if case["kind"] == "terminal":
            return drive.run_terminal(case, lambda: ack_provider)
        world = drive.new_world(case)
        orch = common.build_offline_orchestrator(meter, registry=world.registry, planner_tier=tier,
                                                 selections={"operate-a-quest": "orchestrator"})
        orch.web = world.web
        return drive.run_with(case, world, orch)
    return turn
