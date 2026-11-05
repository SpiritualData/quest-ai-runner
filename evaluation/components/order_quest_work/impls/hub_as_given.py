"""The hub's ``as_given`` arm of Prioritization, answering Quest's cases: the order given, the bound both ordering arms have to beat. Zero model calls."""
from hubofhubs.api import AssemblyConfig, assemble

FOUNDATION_ARMS = {"hub_cases": "as_given"}


def build(meter):  # noqa: ARG001
    arm = assemble(AssemblyConfig(solutions=("prioritization",), selections={"prioritization": "as_given"}))["prioritization"]
    return lambda kind, args: [item["id"] for item in arm.prioritize(person=args["person"], candidates=args["candidates"], stated=args.get("stated"), now=args["now"])["ordered"]]
