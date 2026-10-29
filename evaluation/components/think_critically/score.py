"""Score one request: was the relevant metric applied to it by code, and was what the code found answered?

The labels decide, never the arm's words. A case passes when:
  * it has a metric: the rows hold a measured before and after equal to the true readings (a hidden or invented reading
    fails), and when the true reading got worse a response (retry, revert, follow_up or escalate) is on record;
  * it has no metric and changed something: a metric was proposed and no reading was invented;
  * it has no metric and changed nothing: no reading was invented (nothing is asked of the system).
``aggregate`` prints the share held, per kind of request, so a system that holds only deep runs reads apart from one that
holds every request."""

RESPONSES = ("retry", "revert", "follow_up", "escalate")


def problems_of(got, case):
    rows = got.get("rows") or []
    out = []
    if case["has_metric"]:
        pairs = [(r.get("before"), r.get("after")) for r in rows if r.get("source") == "measured"]
        if (case["before"], case["after"]) not in pairs:
            out.append(f"no measured row of {case['before']} -> {case['after']} (rows {pairs or 'none'})")
        if case["after"] < case["before"] and got.get("response") not in RESPONSES:
            out.append("the metric got worse and nothing answered it")
    else:
        if rows:
            out.append("a reading was kept for a request with no metric")
        if case["changed"] and not got.get("proposed"):
            out.append("a change with no metric and no metric proposed")
    return out


def score_case(fn, case):
    got = fn(case)
    problems = problems_of(got, case)
    return dict(ok=not problems, kind=case["kind"], got=f"{len(got.get('rows') or [])} rows",
                note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    by = {}
    for r in rows:
        k = by.setdefault(r.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
    held = sum(1 for r in rows if r.get("ok"))
    return f"held {held}/{len(rows)} requests (" + ", ".join(f"{k} {v[1]}/{v[0]}" for k, v in sorted(by.items())) + ")"
