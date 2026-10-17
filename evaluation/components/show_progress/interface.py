"""The progress-policy function. Every solution is ONE function with this signature:

    surface(kind: str, args: dict) -> dict

The orchestrator emits the same events in both product modes and never decides what a person sees; the
progress sink does. The contract: a LIVE (attended) run shows every event, in order; a BACKGROUND run
shows only results, decisions, milestones and the done marker (no planning, reading, status or partial
chatter); detaching mid-run switches the rest of the run to the background policy; and a broken stream or
callback never stops the run. Offline: the sinks are pure policy, the only "edge" is the list that
collects what a person would have seen.

Kinds, args and the dict a solution returns:
  surface  args {mode: "live"|"background", events: [event type, ...], broken: bool}
           -> {"surfaced": event types that reached the person, in order, "survived": the run did not raise}
           ``broken`` makes the person-facing callback raise on every call.
  handoff  args {events, detach_after: n}   the first n events are sent attended, then the consumer detaches
           -> {"surfaced": [...], "survived": true}

An implementation module in ``impls/`` exposes ``build(meter) -> surface``. No solution here calls a model.
"""
