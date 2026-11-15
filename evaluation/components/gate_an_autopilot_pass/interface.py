"""The autopilot run gate function. Every solution is ONE function with this signature:

    gate(case_args: dict) -> dict

The backend schedules each run of a named autopilot and queues one ``autopilot_run`` task; the runner decides, before any goal is read or any model is
called, whether that run goes ahead. This component scores only that decision, with a scripted Quest client and a pinned clock, so it makes no model call.

``case_args`` keys:
  autopilots     the quest's autopilots (each ``id``, ``name``, ``instructions``, ``mode``, optional ``enabled``, ``paused_by_plan``)
  task           the run task (``autopilot_id``, ``occurrence_key``: ``reply:`` or ``extra:`` is an automatic extra run, anything else is scheduled or Run now)
  tasks          the team's tasks (each ``id``, ``status``, ``task_kind``, ``autopilot_id``, ``occurrence_key``, ``created_at``, ``goal_id``)
  quest          quest-level settings (``paused``)
  budget         the team's daily budget for automatic extra runs (default 3)
  backpressure   enable per-autopilot backpressure
  now            the clock, ISO 8601 in UTC

It returns ``{"outcome": "ran" | "skipped", "text": the skip reason or ""}``: "ran" means the run got past every gate.

An implementation module in ``impls/`` exposes ``build(meter) -> gate``.
"""
