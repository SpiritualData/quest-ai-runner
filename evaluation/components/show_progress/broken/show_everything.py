"""BASELINE: no policy. Every event is shown to the person in every mode, and a broken stream is allowed
to raise into the run. The floor the sinks must beat. Zero model calls."""


def build(meter):  # noqa: ARG001
    def surface(kind, args):
        if kind not in ("surface", "handoff"):
            raise ValueError(kind)
        shown = []
        for name in args["events"]:
            if args.get("broken"):
                raise RuntimeError("stream dropped")
            shown.append(name)
        return {"surfaced": shown, "survived": True}
    return surface
