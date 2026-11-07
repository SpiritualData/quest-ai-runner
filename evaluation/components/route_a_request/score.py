"""Score one route-a-request case: does the arm say the route the case expects (information, act, draft or clarify)?

One number: the share of requests routed to the labelled route, with the share per route beside it, and for the private set (when present) its own count. The cases are the invented
public requests of ``cases.json`` plus the private labelled set (``private_routes.py``) when the machine holds it. No row, log or note ever carries request text: a failure names the case id, the
route got and the route expected."""
from route_a_request import private_routes
import json
from pathlib import Path

ROUTES = ("information", "act", "draft", "clarify")


def load_cases():
    here = Path(__file__).resolve().parent
    return json.loads((here / "cases.json").read_text(encoding="utf-8")) + private_routes.load_cases()


def score_case(fn, case):
    got = fn(case["kind"], case["args"])
    ok = got == case["expect"]
    return dict(ok=ok, got=str(got), kind=case["kind"], source=case.get("source") or "public", note="" if ok else f"expected {case['expect']}")


def aggregate(rows):
    out = {f"{kind}_passed": f"{sum(bool(r['ok']) for r in rows if r.get('kind') == kind)}/{sum(1 for r in rows if r.get('kind') == kind)}" for kind in ROUTES}
    private = [r for r in rows if r.get("source") == private_routes.SOURCE]
    out["private_passed"] = f"{sum(bool(r['ok']) for r in private)}/{len(private)}" if private else "private set absent"
    return out
