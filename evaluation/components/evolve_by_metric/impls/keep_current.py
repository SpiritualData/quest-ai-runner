"""FLOOR: never switches and never looks at the snapshot. The selection stays whatever someone last wrote, however the metric
reads, which is what evolving by taste looks like. Zero model calls."""


def build(meter):  # noqa: ARG001
    def evolve(kind, args):  # noqa: ARG001
        if kind == "challenger":
            return {"switch_to": None, "status": "keep" if args["selected"] in args["rows"] else "unmeasured"}
        return {"disagreeing": [], "checked": 0}
    return evolve
