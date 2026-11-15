"""Score one autopilot gate case: run the gate, compare the returned dict with the case's declarative ``expect`` (see
``expect.py``). ``aggregate`` reports pass counts per gate."""
from esoi import expect


def score_case(fn, case):
    try:
        out = fn(case["args"])
    except Exception as e:  # noqa: BLE001  a gate that raises where the contract says "decide" fails the case
        if type(e).__name__ == "BudgetExceeded":
            raise
        return dict(ok=False, got=case["kind"], note=f"raised {type(e).__name__}: {e}")
    problems = expect.check(out, case["expect"])
    return dict(ok=not problems, got=case["kind"], note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
