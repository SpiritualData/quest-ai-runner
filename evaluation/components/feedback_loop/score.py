"""Score one feedback-loop case: every turn must show each ``want`` and none of the ``avoid`` strings.
``aggregate`` reports the repeat-correction rate (relevant turns that got their correction back) and the
leak rate (irrelevant corrections shown to a turn they do not bear on)."""


def score_case(fn, case):
    steps = case["steps"]
    out = fn([dict(s) for s in steps])
    turns = out["turns"]
    turn_steps = [s for s in steps if s["op"] == "turn"]
    if len(turns) != len(turn_steps):
        return dict(ok=False, got=case.get("area"), note=f"{len(turns)} turn outputs for {len(turn_steps)} turns")
    wanted = hit = leaked = avoided = 0
    problems = []
    for step, shown in zip(turn_steps, turns):
        low = shown.lower()
        for w in step.get("want", []):
            wanted += 1
            if w.lower() in low:
                hit += 1
            else:
                problems.append(f"missing {w!r}")
        for a in step.get("avoid", []):
            avoided += 1
            if a.lower() in low:
                leaked += 1
                problems.append(f"leaked {a!r}")
    return dict(ok=not problems, got=case.get("area"), wanted=wanted, hit=hit, avoid_checks=avoided,
                leaked=leaked, note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    wanted = sum(r.get("wanted", 0) for r in rows)
    hit = sum(r.get("hit", 0) for r in rows)
    checks = sum(r.get("avoid_checks", 0) for r in rows)
    leaked = sum(r.get("leaked", 0) for r in rows)
    repeat = (wanted - hit)
    return (f"corrections surfaced when relevant {hit}/{wanted} (repeat-correction misses {repeat}), "
            f"irrelevant corrections kept out {checks - leaked}/{checks}")
