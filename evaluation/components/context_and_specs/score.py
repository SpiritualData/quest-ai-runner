"""Score one mixed card and specification case.

A case names the request (``args.task``, optional ``args.tags``) and what a run needs in view:
  want            groups of keys; each group needs one of its keys among the first ``BUDGET`` items (cards ``card:<id>``,
                  records by id), so an arm that buries the right item under a crowd misses it
  no_solution     true when the best match is a problem or an intent no solution serves: the arm must say so; false
                  (the default) means it must not, because a false "no solution yet" sends a run to write a duplicate
                  solution (R1)
  nothing         true for an unrelated request: the arm must put nothing in view
and every offline arm must make zero model calls. A case marked ``live_only`` (an ambiguous request only a model can
judge) is left out of the offline set by ``load_cases``.

``aggregate`` reports passes per case kind, the cases that found everything within the budget, the "no solution yet"
cases flagged, the unrelated cases left empty, and the model calls."""
import json
from pathlib import Path

BUDGET = 10   # items a run's view holds for one request, ranked and linked together
CASES = Path(__file__).resolve().parent / "cases.json"


def load_cases():
    return [c for c in json.loads(CASES.read_text(encoding="utf-8")) if not c.get("live_only")]


def score_case(fn, case):
    args = case["args"]
    out = fn(args["task"], args.get("tags") or [])
    items = list(out.get("items") or [])
    view = items[:BUDGET]
    problems = []
    missing = [group for group in case.get("want", []) if not any(key in view for key in group)]
    if missing:
        problems.append("not in view within the budget: " + ", ".join("/".join(g) for g in missing))
    flagged = bool(out.get("no_solution_yet"))
    if case.get("no_solution") and not flagged:
        problems.append("did not say 'no solution yet'")
    if flagged and not case.get("no_solution"):
        problems.append("said 'no solution yet' where a solution exists")
    if case.get("nothing") and items:
        problems.append(f"an unrelated request put {len(items)} item(s) in view")
    if out.get("calls", 0):
        problems.append(f"{out['calls']} model call(s) in an offline arm")
    return dict(ok=not problems, kind=case["kind"], found=not missing, flag_ok=flagged == bool(case.get("no_solution")),
                empty_ok=not (case.get("nothing") and items), model_calls=out.get("calls", 0),
                got=f"{len(items)} item(s), route {out.get('route', '')}: {', '.join(view[:6])}{' ...' if len(view) > 6 else ''}",
                note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    kinds = {}
    for r in rows:
        p, t = kinds.get(r.get("kind", "?"), (0, 0))
        kinds[r.get("kind", "?")] = (p + bool(r.get("ok")), t + 1)
    found = sum(1 for r in rows if r.get("found"))
    flags = sum(1 for r in rows if r.get("flag_ok"))
    empty = sum(1 for r in rows if r.get("empty_ok"))
    calls = sum(r.get("model_calls", 0) for r in rows)
    return (f"found within budget {found}/{len(rows)}, 'no solution yet' right {flags}/{len(rows)}, unrelated left empty "
            f"{empty}/{len(rows)}, model calls {calls}; by kind: "
            + ", ".join(f"{k} {p}/{t}" for k, (p, t) in sorted(kinds.items())))
