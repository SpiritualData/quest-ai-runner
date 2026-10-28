"""Score one act_as_the_body case from the receipts: writes that ran, reads that ran, and what the last planner prompt
shows of the account. ``aggregate`` reports repeated changes, stale reads (a read after a change that did not run) and
known-gap cases failing."""


def score_case(fn, case):
    out = fn(case)
    writes = [c.get("value") for c in out["handler_calls"]]
    want_writes = case["expect_writes"]
    problems = []
    repeated = max(0, (len(writes) - len(set(writes))) - (len(want_writes) - len(set(want_writes))))
    if writes != want_writes:
        problems.append(f"writes {writes}, expected {want_writes}")
    stale = 0
    if out["reads"] != case["expect_reads"]:
        problems.append(f"reads {out['reads']}, expected {case['expect_reads']}")
        if out["reads"] < case["expect_reads"]:
            stale = case["expect_reads"] - out["reads"]
    for s in case.get("final_shows", []):
        if s not in out["final_prompt"]:
            problems.append(f"last planner prompt does not show {s!r}")
    return dict(ok=not problems, got=case.get("area"), repeated=repeated, stale=stale,
                known_gap=bool(case.get("known_gap")), note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    repeated = sum(r.get("repeated", 0) for r in rows)
    stale = sum(r.get("stale", 0) for r in rows)
    gaps = sum(1 for r in rows if r.get("known_gap") and not r.get("ok"))
    return (f"repeated changes {repeated}; reads skipped that should have run {stale}; known-gap cases failing {gaps}; "
            f"model calls 0")
