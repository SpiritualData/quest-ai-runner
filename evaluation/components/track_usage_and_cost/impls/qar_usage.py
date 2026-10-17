"""CURRENT: the live code. ``pricing.get_input_cost_per_mtok`` / ``estimate_bootstrap_cost`` /
``get_provider_and_model``, ``usage.DailyUsageTracker`` (with a real usage file in a scratch folder) and
``resources.ResourceGuard`` fed scripted host samples. The host sampler is the only fake. Zero model calls."""
import tempfile
from pathlib import Path

from quest_ai_runner.pricing import estimate_bootstrap_cost, get_input_cost_per_mtok, get_provider_and_model
from quest_ai_runner.resources import ResourceGuard, ResourceLimits, ResourceSnapshot
from quest_ai_runner.usage import DailyUsageLimits, DailyUsageTracker


def daily_budget(args):
    with tempfile.TemporaryDirectory(prefix="qar_usage_") as root:
        path = str(Path(root) / "usage.json")
        limits = DailyUsageLimits.from_env(args.get("env") or {})
        tracker = DailyUsageTracker(path=path, limits=limits)
        for tokens_in, tokens_out in args["calls"]:
            tracker.record(tokens_in, tokens_out)
        if args.get("reload"):
            tracker = DailyUsageTracker(path=path, limits=limits)
        return {"total": tracker.total_tokens(), "over": tracker.over_limit(), "status": tracker.status()}


def resource_guard(args):
    samples = iter(args["snapshots"])
    guard = ResourceGuard(ResourceLimits.from_env(args.get("env") or {}),
                          sampler=lambda: ResourceSnapshot(*next(samples)))
    return {"paused": [guard.check() for _ in args["snapshots"]], "enabled": guard.enabled}


def provider(args):
    name, model, key = get_provider_and_model(args["env"])
    return {"provider": name, "model": model, "key": key}


def estimate(args):
    cost, name, model = estimate_bootstrap_cost(args["tokens"], args["env"])
    return {"cost": round(cost, 6), "provider": name, "model": model}


KINDS = {
    "price": lambda a: {"per_mtok": get_input_cost_per_mtok(a["provider"], a["model"])},
    "estimate": estimate,
    "provider": provider,
    "daily_budget": daily_budget,
    "resource_guard": resource_guard,
}


def build(meter):  # noqa: ARG001
    def track(kind, args):
        return KINDS[kind](args)
    return track
