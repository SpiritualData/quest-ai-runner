"""Score one hub-of-hubs case: the case expects ``holds`` true (an ideal) and the implementation answers from the registry
and the code. ``aggregate`` prints the metric: coverage, integration-by-config share, research freshness, the
code-structure shares, the violations and the combined score."""
from esoi import expect

from hub_of_hubs import registry


def reason(out, kind):
    """A short reason a check failed, from what the implementation returned."""
    return {"registered": f"status {out.get('status')} or no recorded finding",
            "by_config": f"{out.get('by_config')} of {out.get('capabilities')} capabilities by configuration",
            "fresh": f"researched {out.get('days')} days ago",
            "seam": "no declared implementation behind the area's solution",
            "selectable": "the seam solution has no authoritative metric or fewer than two implementations"}.get(kind, "")


def score_case(fn, case):
    try:
        out = fn(case["kind"], case["args"])
    except Exception as exc:  # an implementation that raises fails the case
        if type(exc).__name__ == "BudgetExceeded":
            raise
        return dict(ok=False, got=case["kind"], note=f"raised {type(exc).__name__}: {exc}", out={})
    problems = expect.check(out, case["expect"])
    note = out.get("note") or (out.get("first") if case["kind"] == "core_clean" else "") or reason(out, case["kind"])
    return dict(ok=not problems, got=case["kind"], out=out, area=case["args"]["area"], note=note if problems else "ok")


def aggregate(rows):
    facts, caps = registry.all_facts()
    m = registry.metrics(facts, caps)
    stale = sorted(f["area"] for f in facts if not f["fresh"])
    kinds = {}
    for r in rows:
        done, total = kinds.get(r.get("got"), (0, 0))
        kinds[r.get("got")] = (done + bool(r.get("ok")), total + 1)
    per_kind = ", ".join(f"{k} {d}/{t}" for k, (d, t) in sorted(kinds.items()))
    return (f"checks passed {sum(1 for r in rows if r.get('ok'))}/{len(rows)} ({per_kind}); registry reading "
            f"(the same for every implementation): score {m['score']:.3f} = mean(coverage {m['coverage']:.2f}, "
            f"integration-by-config {m['config_share']:.2f}, freshness {m['fresh_share']:.2f}, code structure "
            f"{m['code']:.2f}); research age max {m['days_max']} mean {m['days_mean']:.1f} days (bar "
            f"{registry.STALENESS_DAYS}), stale areas {stale or 'none'}; code: one seam {m['seam_share']:.2f}, "
            f"selectable by metric {m['selectable_share']:.2f}, core free of vendor branches {m['clean_share']:.2f} "
            f"({m['vendor_branches']} branches)")
