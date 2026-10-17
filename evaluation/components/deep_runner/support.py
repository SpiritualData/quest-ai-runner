"""Shared outcome normalisation for the deep-runner arms: two runners word their errors differently, but
the CONTRACT is the same, so each result is reduced to the facts a caller acts on."""


def normalise(result, want, raised=False):
    if raised or result is None:
        return {"met": False, "has_want": False, "decision_id": None, "error_kind": "failed", "raised": True}
    error = (result.error or "").lower()
    if not error:
        kind = "none"
    elif "no output" in error or "no agent output" in error:
        kind = "no_output"
    elif "not found" in error or "no such file" in error:
        kind = "missing_binary"
    else:
        kind = "failed"
    return {"met": bool(result.met), "has_want": bool(want) and want in (result.output or ""),
            "decision_id": result.decision_id, "error_kind": kind, "raised": False}
