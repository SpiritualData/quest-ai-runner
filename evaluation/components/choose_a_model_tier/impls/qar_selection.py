"""CURRENT: the live selection code. ``select_deep_start``, ``resolve_model_name``, ``ModelRegistry.resolve_tier``
over a provider that only lists models, ``next_lower_tier``, ``is_vision_capable``, ``provider_outage.is_outage``
and ``usage_limit.detect_usage_limit`` / ``UsageLimit.resume_at``. The provider's model list is the only fake.
Zero model calls."""
from datetime import datetime, timezone

from hubofhubs.models import outage as provider_outage
from hubofhubs.models.family import resolve_model_name
from quest_ai_runner.core.deep_model_selection import select_deep_start
from quest_ai_runner.core.model_registry import ModelRegistry, is_vision_capable, next_lower_tier
from quest_ai_runner.core.usage_limit import detect_usage_limit


class ListingProvider:
    """A provider that can only list models: enough for a registry to resolve tiers."""

    def __init__(self, live):
        self.live = list(live)

    def list_models(self):
        return list(self.live)


def start_model(args):
    picked = select_deep_start(args["difficulty"], args["ladder"], args.get("models"))
    if picked is None:
        return {"start": None, "ladder": list(args["ladder"])}
    ladder, _ = picked
    return {"start": ladder[0], "ladder": ladder}


def tier(args):
    registry = ModelRegistry(ListingProvider(args["live"]), fallback=args.get("pins") or None)
    return {"model": registry.resolve_tier(args["tier"])}


def usage_limit(args):
    now = datetime.fromisoformat(args["now"]).replace(tzinfo=timezone.utc)
    found = detect_usage_limit(args["text"], now)
    if found is None:
        return {"limited": False, "kind": "", "resets_at": None, "resume_at": None}
    return {"limited": True, "kind": found.kind, "resets_at": found.resets_at.isoformat() if found.resets_at else None,
            "resume_at": found.resume_at(1, now).isoformat()}


KINDS = {
    "start_model": start_model,
    "family": lambda a: {"model": resolve_model_name(a["model"], a["live"])},
    "tier": tier,
    "lower_tier": lambda a: {"tier": next_lower_tier(a["tier"])},
    "vision": lambda a: {"vision": is_vision_capable(a["model"])},
    "outage": lambda a: {"outage": provider_outage.is_outage(RuntimeError(a["message"]))},
    "usage_limit": usage_limit,
}


def build(meter):  # noqa: ARG001
    def choose(kind, args):
        return KINDS[kind](args)
    return choose
