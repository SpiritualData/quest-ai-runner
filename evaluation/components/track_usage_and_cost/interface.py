"""The usage-and-cost function. Every solution is ONE function with this signature:

    track(kind: str, args: dict) -> dict

A lane has to know what a run will cost before it spends, stop at a daily token budget, and stop taking work
when its host is overloaded. The three pieces are deterministic once their outside seams (the clock-free token
log and the host sampler) are fed by the case, so this component runs offline, with no model.

Kinds and the dict a solution returns:
  price          args {provider, model}              -> {"per_mtok": USD per million input tokens | None}
  estimate       args {tokens, env}                  -> {"cost": USD, "provider": name, "model": id}
  provider       args {env}                          -> {"provider": name, "model": id, "key": "configured" | "missing" | None}
  daily_budget   args {env, calls, reload}           -> {"total": tokens today, "over": bool, "status": str}
                 ``calls`` is [[tokens_in, tokens_out], ...] recorded in order; ``reload`` builds a second tracker
                 on the same usage file (a restart) and reports that one
  resource_guard args {env, snapshots}               -> {"paused": [bool per check], "enabled": bool}
                 ``snapshots`` is [[memory_percent, free_memory_mb, load_per_core], ...], one per check; null = unreadable
An implementation module in ``impls/`` exposes ``build(meter) -> track``. No solution here calls a model.
"""
