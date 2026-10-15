"""Score one liveness case: ``ok`` when the returned ``stop`` equals ``want_stop``. A verdict of None never
passes. ``aggregate`` splits the errors, because the two directions cost differently: a FALSE STOP kills work
that was about to land (the expensive error), a missed stop wastes time until the next review."""


def score_case(fn, case):
    out = fn(case["elapsed"], case["idle"], case["tail"]) or {}
    stop = out.get("stop")
    want = case["want_stop"]
    return dict(ok=stop is want, stop=stop, want=want, got=f"stop={stop}",
                note="ok" if stop is want else f"want stop={want}, got {stop}: {out.get('reason', '')}")


def aggregate(rows):
    false_stops = sum(1 for r in rows if r.get("stop") is True and r.get("want") is False)
    missed = sum(1 for r in rows if r.get("stop") is not True and r.get("want") is True)
    keep = sum(1 for r in rows if r.get("want") is False)
    stop = sum(1 for r in rows if r.get("want") is True)
    return f"false stops {false_stops}/{keep} keep-running logs, missed stops {missed}/{stop} stop logs"
