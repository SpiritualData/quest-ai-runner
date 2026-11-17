"""CURRENT: the production ``readable_guard`` that the outward write tools call. Zero model calls, zero network."""
from quest_ai_runner.world.tools.readable_guard import machine_strings


def build(meter):  # noqa: ARG001
    def check(kind, args):
        if kind != "outward":
            raise ValueError(kind)
        named = machine_strings(*args["texts"])
        return {"refused": bool(named), "named": named}
    return check
