"""ALTERNATIVE (the floor): the hub's always-information arm. Zero model calls: the bound the others have to beat."""
from hubofhubs.api import MajorityFloor


def build(meter):  # noqa: ARG001
    arm = MajorityFloor()
    return lambda kind, args: arm.route(args["request"], args.get("context") or None)["route"]
