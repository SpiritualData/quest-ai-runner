"""ALTERNATIVE: the hub's own ``stated_fields`` arm of Prioritization, answering Quest's cases: the alternative the Quest arm has to beat. It reads only the fields any
data carries, so a rule only Quest's data has (a person's rank map, a goal's horizon) is not found. Zero model calls."""
from hubofhubs.api import AssemblyConfig, assemble

FOUNDATION_ARMS = {"hub_cases": "stated_fields"}


def build(meter):  # noqa: ARG001
    arm = assemble(AssemblyConfig(solutions=("prioritization",), selections={"prioritization": "stated_fields"}))["prioritization"]
    return lambda kind, args: [item["id"] for item in arm.prioritize(person=args["person"], candidates=args["candidates"], stated=args.get("stated"), now=args["now"])["ordered"]]
