"""BASELINE: no selection logic. Every deep run starts on the strongest rung, a family name is used as written,
every tier is the first live model, a tier that is exhausted has no cheaper fallback, vision is "the id contains
claude", an outage is any message with a 5xx number, and a usage limit is any message containing the word limit,
with no reset time read. The floor a difficulty-aware, family-aware choice must beat. Zero model calls."""
import re
from datetime import datetime, timedelta, timezone


def build(meter):  # noqa: ARG001
    def choose(kind, args):
        if kind == "start_model":
            ladder = list(args["ladder"])
            return {"start": ladder[-1] if ladder else None, "ladder": ladder[-1:]}
        if kind == "family":
            return {"model": args["model"]}
        if kind == "tier":
            return {"model": (args["live"] or ["none"])[0]}
        if kind == "lower_tier":
            return {"tier": None}
        if kind == "vision":
            return {"vision": "claude" in args["model"].lower()}
        if kind == "outage":
            return {"outage": bool(re.search(r"\b5\d\d\b", args["message"]))}
        if kind == "usage_limit":
            limited = "limit" in args["text"].lower()
            now = datetime.fromisoformat(args["now"]).replace(tzinfo=timezone.utc)
            return {"limited": limited, "kind": "", "resets_at": None,
                    "resume_at": (now + timedelta(minutes=30)).isoformat() if limited else None}
        raise ValueError(kind)
    return choose
