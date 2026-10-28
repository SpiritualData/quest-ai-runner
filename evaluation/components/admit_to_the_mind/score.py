"""Score one admit_to_the_mind case: which labelled items reached the view the next step is given.
``aggregate`` reports recall of needed items, near misses admitted and out-of-scope items admitted."""


def score_case(fn, case):
    view = fn(case)["view"]
    needed = [i for i in case["items"] if i["label"] == "needed"]
    near = [i for i in case["items"] if i["label"] == "near_miss"]
    out = [i for i in case["items"] if i["label"] == "out_of_scope"]
    got_needed = [i for i in needed if i["marker"] in view]
    got_near = [i for i in near if i["marker"] in view]
    got_out = [i for i in out if i["marker"] in view]
    problems = []
    if len(got_needed) < len(needed):
        problems.append(f"{len(needed) - len(got_needed)} needed item(s) not admitted")
    if got_out:
        problems.append(f"{len(got_out)} out-of-scope item(s) admitted")
    if len(got_near) > case.get("near_miss_allowed", 0):
        problems.append(f"{len(got_near)} near miss(es) admitted")
    return dict(ok=not problems, got=case.get("area"), needed=len(needed), needed_admitted=len(got_needed),
                near=len(near), near_admitted=len(got_near), out=len(out), out_admitted=len(got_out),
                note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    n = sum(r.get("needed", 0) for r in rows)
    na = sum(r.get("needed_admitted", 0) for r in rows)
    nm = sum(r.get("near", 0) for r in rows)
    nma = sum(r.get("near_admitted", 0) for r in rows)
    o = sum(r.get("out", 0) for r in rows)
    oa = sum(r.get("out_admitted", 0) for r in rows)
    return (f"needed items admitted {na}/{n}; near misses admitted {nma}/{nm}; out-of-scope items admitted {oa}/{o}; "
            f"model calls per step 0")
