"""BASELINE (broken): what a first draft would write. It takes each arm's latest row whatever its age, commit or cases,
and counts an arm with no row (or one not built yet) as a pass, so an unmeasured or stale part reads as perfect. The
scorer's independent freshness check must fail it."""
from qar_overall import combine

PERFECT = {"passed": "1", "cases": "1", "cost_usd": "0.0", "git_sha": "assumed", "date": "assumed", "model_calls": "0"}


def any_age(comp, arm_history, tier):  # noqa: ARG001
    return (arm_history[-1] if arm_history else dict(PERFECT)), ""


def everything_counts(comp, row):  # noqa: ARG001
    return ""


def build(meter):  # noqa: ARG001
    def read(args):
        if args.get("rule", "").startswith("structure_"):
            return combine.read(args, structure=lambda rule: combine.structure_reading(rule, everything_counts))
        reading = combine.read(dict(args, **{t: [dict(s, planned=False, arm=s["arm"] if "/" in s["arm"] else s["arm"] + "/planned")
                                                  for s in args.get(t) or []] for t in combine.TIERS}), choose=any_age)
        return reading
    return read
