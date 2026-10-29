"""BASELINE: what a first draft writes. The rival with the highest raw pass count replaces the selection, whatever cases it
was scored on, however small the gap, and whatever it costs; the snapshot is trusted as shipped."""


def build(meter):  # noqa: ARG001
    def evolve(kind, args):
        if kind == "challenger":
            rows = args["rows"]
            if args["selected"] not in rows:
                return {"switch_to": None, "status": "unmeasured"}
            best = max(rows, key=lambda impl: rows[impl]["passed"] / max(rows[impl]["cases"], 1))
            same = best == args["selected"]
            return {"switch_to": None if same else best, "status": "keep" if same else "switch"}
        return {"disagreeing": [], "checked": 0}
    return evolve
