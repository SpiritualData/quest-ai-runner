"""FLOOR: an assistant with no feedback loop. A correction is heard and forgotten, so no later turn is shown
it. It never leaks an irrelevant correction and never repeats a relevant one: the floor the loop must beat on
the repeat-correction rate. Zero model calls."""


def build(meter):  # noqa: ARG001
    def replay(steps):
        return {"turns": ["" for s in steps if s["op"] == "turn"]}
    return replay
