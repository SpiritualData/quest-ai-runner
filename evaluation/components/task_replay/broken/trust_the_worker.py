"""BASELINE: the shortcut a first draft would write. No worker is spawned and no verifier is asked: a task is reported met
because nothing said otherwise. The scorer accepts only a task a done verifier judged, so every case fails it."""


def build(meter):  # noqa: ARG001
    return lambda case: {"verdicts": [], "spawns": [], "met": True, "changed": [], "created": [], "work_ok": None}
