"""BASELINE: the obvious inbox written without the contract: one list for every conversation, drain returns
everything and does not clear it, blanks are kept. Wired into the live Orchestrator, it delivers (so it
passes the easy cases) but leaks one conversation's messages into another and repeats a message on every
step. Shows the metric separates a careful inbox from a naive one. Zero model calls."""
import receive_input_mid_run.driver as driver


class SharedListInbox:
    def __init__(self):
        self.items = []

    def push(self, conversation_id, message):  # noqa: ARG002
        self.items.append(message)

    def drain(self, conversation_id):  # noqa: ARG002
        return list(self.items)

    def peek(self, conversation_id):  # noqa: ARG002
        return list(self.items)


def build(meter):  # noqa: ARG001
    def receive(kind, args):
        inbox = SharedListInbox()
        return driver.run_case(args, inbox) if kind == "run" else driver.inbox_ops(args, inbox)
    return receive
