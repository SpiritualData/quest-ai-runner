"""Score one hold_the_long_view case: every ``want`` in the first planner prompt, no ``avoid`` in it.
``aggregate`` reports declines carried, items that crossed a scope they should not have, and known-gap cases failing."""


def score_case(fn, case):
    prompt = fn(case).get("prompt", "")
    missing = [w for w in case.get("want", []) if w not in prompt]
    leaked = [a for a in case.get("avoid", []) if a in prompt]
    ok = not missing and not leaked
    return dict(ok=ok, got=case.get("area"), wanted=len(case.get("want", [])), carried=len(case.get("want", [])) - len(missing),
                leaked=len(leaked), known_gap=bool(case.get("known_gap")),
                note="ok" if ok else "; ".join([f"not carried {m!r}" for m in missing] + [f"leaked {a!r}" for a in leaked]))


def aggregate(rows):
    wanted = sum(r.get("wanted", 0) for r in rows)
    carried = sum(r.get("carried", 0) for r in rows)
    leaked = sum(r.get("leaked", 0) for r in rows)
    gaps = sum(1 for r in rows if r.get("known_gap") and not r.get("ok"))
    return (f"earlier items carried to the next request {carried}/{wanted}; items that crossed a scope they should not "
            f"{leaked}; known-gap cases failing {gaps}; time added before the reply 0; model calls 0")
