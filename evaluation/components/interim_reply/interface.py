"""The interim-reply and ask function (metric ``metric-interim-reply-truthful-and-useful``). Every solution is ONE function:

    run(kind: str, args: dict) -> dict

Kinds, args and the dict returned:
  turn  args {steps: [one dict per goal-loop attempt that did not meet the goal]}   -> {"sent": indices of the attempts that sent an interim}
        a step is {met, blocker (more_work | evidence_only | needs_person | tool_failure), loop_runs_again, ask_open, has_why,
        has_content, at (seconds into the turn), signature (any string; equal strings mean nothing new since the last attempt)}
  ask   args {blocker, question, failures: [ledger failure signatures], asked_of, member_ref, participants, asker,
        turn_id, conv_id, gap, later_gap}                                           -> {"ask": bool, "assignee": user id, "asked_of": "asker"|"member", "same_key": bool}

The cases are labelled by construction (a missing credential is blocked on a person, a tool fault is not), so the checks run on the turn
record, never on the wording of a reply: more-coming truth and no dangling end follow from sending only before another attempt, ask iff
blocked is precision and recall of ``ask``, delta and bounds are the signature, spacing and cap cases. No arm calls a model. The tiers on
real turns (haiku through the claude CLI) need the orchestrator switch ``interim_replies`` on and are not part of this offline component.
An implementation module in ``impls/`` exposes ``build(meter) -> run``.
"""
