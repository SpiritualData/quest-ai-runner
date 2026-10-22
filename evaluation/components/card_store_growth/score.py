"""Score one card-store-growth case against its declarative ``expect`` (``esoi.expect``) and keep what the
solution returned, so ``aggregate`` can state the numbers the metric is read in: invariant violations over the
random worlds, cards left beyond what the corpus calls for after one refresh of a legacy store, and whether the
card limit held and said so."""
from esoi import expect


def score_case(fn, case):
    try:
        out = fn(case["kind"], case["args"])
    except Exception as exc:  # a solution that raises where the contract says "report" fails the case
        if type(exc).__name__ == "BudgetExceeded":
            raise
        return dict(ok=False, got=case["kind"], note=f"raised {type(exc).__name__}: {exc}", out={})
    problems = expect.check(out, case["expect"])
    return dict(ok=not problems, got=case["kind"], out=out,
                note="ok" if not problems else "; ".join(problems) + (f" ({out['first']})" if out.get("first") else ""))


def aggregate(rows):
    def total(kind, key):
        return sum(r.get("out", {}).get(key, 0) for r in rows if r.get("got") == kind)

    def count(kind):
        return sum(1 for r in rows if r.get("got") == kind)
    fuzz_bad = sum(1 for r in rows if r.get("got") == "fuzz" and r.get("out", {}).get("violations"))
    limit_kinds = ("limit", "limit_learned", "limit_open")
    held = sum(1 for r in rows if r.get("got") in limit_kinds and r.get("ok"))
    return (f"invariant violations {total('fuzz', 'violations')} over {count('fuzz')} random worlds ({fuzz_bad} worlds broke one); "
            f"cards left beyond the corpus after one refresh of a legacy store {total('legacy', 'excess')} "
            f"(of {total('legacy', 'before') - total('legacy', 'clean')} planted); "
            f"limit cases held {held}/{sum(count(k) for k in limit_kinds)}")
