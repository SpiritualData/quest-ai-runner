"""CURRENT: the live ``InMemoryInbox`` wired into the live Orchestrator (``input_inbox=``), which drains it
between planner steps. Zero model calls."""
import receive_input_mid_run.driver as driver
from quest_ai_runner.core.inbox import InMemoryInbox


def build(meter):  # noqa: ARG001
    def receive(kind, args):
        inbox = InMemoryInbox()
        return driver.run_case(args, inbox) if kind == "run" else driver.inbox_ops(args, inbox)
    return receive
