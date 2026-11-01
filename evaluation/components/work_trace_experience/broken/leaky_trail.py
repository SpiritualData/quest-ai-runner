"""BASELINE: the shortcut a first draft would write. The plain list for the sender, and every participant of a group is sent the sender's
view, record titles included. Zero model calls: the gate rejects it on every group turn that has records."""
from work_trace_experience import arms


def build(meter):  # noqa: ARG001
    def experience(case):
        events = case["events"]
        variant = [dict(name=n, events=events, shared=False) for n in ("sender_live", "sender_history", "other_live", "other_history")]
        return dict(arm="leaky", views=arms.bridge("plain_list", variant), provider=None)
    return experience
