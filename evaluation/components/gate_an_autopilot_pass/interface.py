"""The autopilot gate function. Every solution is ONE function with this signature:

    gate(case_args: dict) -> dict

Autopilot is a recurring pass that decides, per quest, whether to do work now. Before any goal is read or any model is
called it applies cheap gates: the team's daily budget, the quest's cadence, a pending "Run now" request, reactive
mode's "anything new since the last pass", the roster day rule, and (when enabled) backpressure from the quest's own
open autopilot work. This component scores only that decision, with a scripted Quest client and a pinned clock, so
it makes no model call.

``case_args`` keys (all optional except ``cfg``):
  cfg            the quest's autopilot settings (mode, cadence, last_pass_at, run_requested_at, personas,
                 run_timezone)
  now            the pass clock, ISO 8601 in UTC
  tasks          the team's tasks (each ``id``, ``status``, ``task_kind``, ``created_at``, ``goal_id``)
  asks           the quest's ask rows, or null when the read fails
  budget         the team's daily budget (default 3)
  backpressure   enable the backpressure gate

It returns ``{"outcome": "ran" | "skipped", "text": the skip reason or ""}``: "ran" means the pass got past every gate.

An implementation module in ``impls/`` exposes ``build(meter) -> gate``.
"""
