"""BROKEN: the naive router a first draft would write. It ignores the judge and gives every goal to the first autopilot on duty: no scope, no rep's goal,
no goal left alone, no check that the goal is eligible. A test proves it fails the routing cases. Zero model calls."""


def build(meter):  # noqa: ARG001
    def route(args):
        first = args["duty"][0]["id"] if args["duty"] else None
        return {"assignments": {g["id"]: first for g in args["goals"]} if first else {}}
    return route
