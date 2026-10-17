"""BASELINE: the cost model a first draft would use. One flat $3 per million tokens for every provider and model,
the provider is always "anthropic", no daily budget is ever enforced (usage is counted in memory and never over),
and the host is never considered overloaded. The floor that per-model pricing, a persisted budget and a
hysteresis guard must beat. Zero model calls."""


def build(meter):  # noqa: ARG001
    def track(kind, args):
        if kind == "price":
            return {"per_mtok": 3.0}
        if kind == "estimate":
            return {"cost": round(args["tokens"] / 1_000_000 * 3.0, 6), "provider": "anthropic", "model": "sonnet"}
        if kind == "provider":
            return {"provider": "anthropic", "model": "sonnet", "key": None}
        if kind == "daily_budget":
            total = sum(i + o for i, o in args["calls"])
            return {"total": 0 if args.get("reload") else total, "over": False, "status": f"{total} tokens today"}
        if kind == "resource_guard":
            return {"paused": [False for _ in args["snapshots"]], "enabled": False}
        raise ValueError(kind)
    return track
