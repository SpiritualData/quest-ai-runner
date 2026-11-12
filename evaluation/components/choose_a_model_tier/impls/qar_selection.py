"""CURRENT: the live selection code. ``plan_start``, ``resolve_model_name``, ``ModelRegistry.resolve_tier``
over a provider that only lists models, ``next_lower_tier``, ``is_vision_capable``, ``provider_outage.is_outage``
and ``usage_limit.detect_usage_limit`` / ``UsageLimit.resume_at``. The provider's model list is the only fake.
Zero model calls."""
from datetime import datetime, timezone

from hubofhubs.api import outage as provider_outage
from hubofhubs.api import resolve_model_name
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.deep_model_selection import normalize_difficulty, plan_start
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry, is_vision_capable, next_lower_tier
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.usage_limit import detect_usage_limit


class ListingProvider:
    """A provider that can only list models: enough for a registry to resolve tiers."""

    def __init__(self, live):
        self.live = list(live)

    def list_models(self):
        return list(self.live)


TIER_MODELS = {"fast": "haiku", "balanced": "sonnet", "quality": "opus", "best": "opus"}


class FixedTiers:
    """A registry whose tiers are fixed, so the start the hub's rule picks is visible as a model."""

    def resolve_tier(self, name):
        return TIER_MODELS[name]


def start_model(args):
    ladder = list(args["ladder"])
    if not ladder:
        return {"start": None, "ladder": []}
    level = normalize_difficulty(args["difficulty"])
    plan = plan_start(FixedTiers(), configured_ladder=ladder, difficulty=args["difficulty"],
                      operator_model=(args.get("models") or {}).get(level or "normal"))
    picked = plan.ladder if plan else ladder
    return {"start": picked[0], "ladder": picked}


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
