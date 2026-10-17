"""FLOOR: no inbox is wired. The live Orchestrator runs with ``input_inbox=None`` and the interface's pushes
go nowhere, so a message typed while the run works is never seen. What any inbox must beat. Zero model
calls."""
import receive_input_mid_run.driver as driver


def build(meter):  # noqa: ARG001
    def receive(kind, args):
        return driver.run_case(args, None) if kind == "run" else driver.inbox_ops(args, None)
    return receive
