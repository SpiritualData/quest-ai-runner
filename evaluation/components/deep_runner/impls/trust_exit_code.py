"""BASELINE: the worker wrapper a first draft would write. The goal is met whenever the process exits 0, the
output is whatever it printed (even nothing), a human-decision marker is just text, a missing binary or a crash
raises into the caller, and a non-zero exit is reported as a plain failure. The floor both real runners must
beat. Zero model calls."""


def build(meter):  # noqa: ARG001
    def work(kind, args):
        if kind != "run_worker":
            raise ValueError(kind)
        scenario = args["scenario"]
        if scenario.get("spawn_error") or scenario.get("crash"):
            return {"met": False, "has_want": False, "decision_id": None, "error_kind": "failed", "raised": True}
        output = scenario.get("output", "")
        met = scenario.get("exit", 0) == 0
        want = args.get("want")
        return {"met": met, "has_want": bool(want) and want in output, "decision_id": None,
                "error_kind": "none" if met else "failed", "raised": False}
    return work
