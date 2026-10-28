"""Score one replayed task: accepted when the done verifier's first verdict is met. ``work_ok`` (the source test's own
condition on the folder) is reported beside it and counts as a wrong accept when it fails after an accept. A run whose
worker never produced a verdict is not accepted, and a run that made no model call is an error, never a score."""


def score_case(fn, case):
    out = fn(case)
    verdicts = out.get("verdicts") or []
    spawns = out.get("spawns") or []
    if not spawns:
        return dict(ok=False, note="no worker was spawned: nothing was replayed", accepted=False, work_ok=None, attempts=0,
                    verdicts=0, wrong_accept=False, created=[], got="no run")
    first = verdicts[0] if verdicts else None
    accepted = bool(first and first["met"] is True)
    work_ok = out.get("work_ok")
    wrong = accepted and work_ok is False
    created = out.get("created") or []
    note = "ok" if accepted else (f"verifier: {first['reason'][:100]}" if first else "no verdict from the done verifier")
    if wrong:
        note += "; accepted but the source test's condition on the folder fails"
    return dict(ok=accepted, accepted=accepted, work_ok=work_ok, wrong_accept=wrong, attempts=len(spawns),
                verdicts=len(verdicts), created=created, metric_missing=bool(out.get("metric_missing")),
                metrics=len(out.get("metric_results") or []), list_cost=out.get("cli_list_cost_usd", 0.0),
                got=f"first verdict {'met' if accepted else 'not met'}, {len(spawns)} worker run(s)", note=note)


def aggregate(rows):
    n = len(rows) or 1
    acc = sum(1 for r in rows if r.get("accepted"))
    checked = [r for r in rows if r.get("work_ok") is not None]
    good = sum(1 for r in checked if r["work_ok"])
    wrong = sum(1 for r in rows if r.get("wrong_accept"))
    spawns = sum(r.get("attempts", 0) for r in rows)
    made = sum(len(r.get("created") or []) for r in rows)
    miss = sum(1 for r in rows if r.get("metric_missing"))
    return (f"accepted on the first attempt {acc}/{n}; work check met {good}/{len(checked)}; wrong accepts {wrong}; "
            f"worker runs {spawns} ({spawns / n:.2f} per task); files created {made}; metric missing {miss}; "
            f"CLI list-price cost ${sum(r.get('list_cost', 0.0) for r in rows):.2f} (a subscription, not a charge)")
