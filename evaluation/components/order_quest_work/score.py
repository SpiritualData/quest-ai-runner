"""Scores a Quest ordering arm: do the candidates come back in the order the rule of the case gives them?

``metric-order-quest-work`` BUILDS ON the hub's ``prioritization`` metric (``esoi.toml`` ``[builds_on.order_quest_work.hub_cases]``): the hub's invented,
neutral cases, each exercising one generic rule, are the foundation and are not copied here. What stays here is what only Quest can say: the thirteen cases
that exercise Quest's own data and rules (a quest list with a person's rank map and the creator's legacy rank, the autopilot's order of asks, goals and
ideas, goal horizons and the goal ladder, open asks oldest first). Each carries its ``source`` (the Quest test or rule it was taken from). One number: the
share of cases whose whole order equals the reviewed order, balanced with cost and time by ``[balance.order_quest_work]`` (weights and budgets fixed before
any arm was scored). No model call, no network. ``build_cases.py`` rebuilds ``cases.json``."""
import json
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_cases():
    """Only Quest's own cases: the hub's are the foundation's."""
    return json.loads((HERE / "cases.json").read_text())


def foundation_fn(name, fn):
    """The function the hub's scorer calls for this arm: the hub's cases are answered by the same ``order(kind, args)`` function."""
    return fn if name == "hub_cases" else None


def agreement(got, want):
    """The share of pairs of wanted items that ``got`` places in the wanted order (a missing item counts as out of order)."""
    pairs = list(combinations(want, 2))
    if not pairs:
        return 1.0
    place = {ident: n for n, ident in enumerate(got)}
    return sum(a in place and b in place and place[a] < place[b] for a, b in pairs) / len(pairs)


def score_case(fn, case):
    got = list(fn(case["kind"], case["args"]))
    ok = got == case["expect"]
    return dict(ok=ok, got=got if not ok else "the reviewed order", kind=case["kind"], agreement=round(agreement(got, case["expect"]), 4),
                note="ok" if ok else f"got {got}, wanted {case['expect']}")


def aggregate(rows):
    kinds = sorted({r.get("kind") for r in rows})
    out = [f"{k} {sum(bool(r['ok']) for r in rows if r.get('kind') == k)}/{sum(r.get('kind') == k for r in rows)}" for k in kinds]
    out.append(f"mean pairwise agreement {sum(r.get('agreement', 0.0) for r in rows) / max(len(rows), 1):.4f}")
    return "case source quest | " + " | ".join(out)
