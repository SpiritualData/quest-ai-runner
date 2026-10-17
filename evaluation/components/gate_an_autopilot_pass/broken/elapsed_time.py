"""BROKEN: the naive gate a first draft would write. It runs when enough wall-clock time has passed since the last
pass (a fixed interval per cadence) and nothing else: no calendar-period cadence, no roster day rule, no "Run now",
no reactive "anything new" check, no daily budget, no backpressure. A test proves it fails the gate cases. Zero model
calls."""
from datetime import datetime, timedelta

INTERVAL = {"daily": timedelta(hours=24), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}


def parse(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def build(meter):  # noqa: ARG001
    def gate(args):
        cfg = args["cfg"]
        last = cfg.get("last_pass_at")
        wait = INTERVAL.get(cfg.get("cadence"), INTERVAL["daily"])
        if last and parse(args["now"]) - parse(last) < wait:
            return {"outcome": "skipped", "text": "cadence: not enough time has passed"}
        return {"outcome": "ran", "text": ""}
    return gate
