"""Score one card-store-growth case against its declarative ``expect`` (``esoi.expect``) and keep what the
solution returned, so ``aggregate`` can state the numbers the metric is read in: invariant violations over the
random worlds, cards left beyond what the corpus calls for after one refresh of a legacy store, whether the
card limit held and said so, whether what a person or a run added survived, and whether several processes
sharing one store kept every file and counted every move once."""
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
    limit_kinds = ("limit", "limit_learned", "limit_open", "limit_protected", "fresh_bootstrap_cost")
    kept_kinds = ("tidy_protected", "stale_writer", "mounted_edit", "old_copy_with_notes", "moved_corpus")
    process_kinds = ("concurrent_tidy", "concurrent_limit")

    def held(kinds):
        return sum(1 for r in rows if r.get("got") in kinds and r.get("ok"))

    def of(kinds):
        return sum(count(k) for k in kinds)
    return (f"invariant violations {total('fuzz', 'violations')} over {count('fuzz')} random worlds ({fuzz_bad} worlds broke one); "
            f"cards left beyond the corpus after one refresh of a legacy store {total('legacy', 'excess')} "
            f"(of {total('legacy', 'before') - total('legacy', 'clean')} planted); "
            f"limit cases held {held(limit_kinds)}/{of(limit_kinds)}; "
            f"cases where what a person or a run added must survive held {held(kept_kinds)}/{of(kept_kinds)}; "
            f"cases with several processes held {held(process_kinds)}/{of(process_kinds)}")
