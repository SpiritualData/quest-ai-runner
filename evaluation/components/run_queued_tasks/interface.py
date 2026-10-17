"""The queued-task runner function. Every solution is ONE function with this signature:

    run(kind: str, args: dict) -> dict

A lane polls Quest for due tasks, claims each, runs it through the brain, and reports the outcome. This
component scores that lifecycle offline: the one outside seam is a fake Quest client (a dict of tasks and a
log of every call made on it) plus a scripted model, so no network and no model is touched. Everything
between those two edges is the live runner code.

Kinds and the dict a solution returns:
  task_lifecycle  args {tasks, decisions, scans, claim, escalation_id, deep, configured, team_id}
                  -> {"handled": [ids across all scans], "claimed": [ids claim() was called with],
                      "reports": [[id, status], ...], "decision_ids": [...]}
                  ``tasks`` are Quest task dicts (``due_in_minutes`` makes a scheduled task); ``decisions`` is the
                  scripted planner replies; ``claim`` is "ok" or "fail" (another worker holds it); ``deep`` is
                  {met, output, error} for a stub deep runner; ``scans`` is how many times the poller scans.
  persona_select  args {registry, task, cache_dir}
                  -> {"persona": id | None, "skill": slug | None}
  folder_sync     args {state, notes, script}
                  -> {"text": the sync file at the end, "results": per-step counts, "error": exception name | None}
                  ``script`` steps: "pull", "push", {"append": text} (a human edit), {"bullet": text} (a note to push).
  folder_owner    args {map, path} -> {"quest": quest_id | None}   (which quest a directory belongs to)
  lane_clock      args {date, time, zone}   -> {"iso": ISO string | None, "aware": bool}
  lane_env_file   args {lines, preset}      -> {"env": {key: value} after loading}
  lane_args       args {argv}               -> {"once", "check", "verbose"}

An implementation module in ``impls/`` exposes ``build(meter) -> run``. No solution here calls a model.
"""
