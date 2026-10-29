"""CURRENT, live: the esoi library's own leaderboard decides (``leaderboard.challengers``: same cases and sample, beyond the noise
margin, or as good and clearly cheaper), and the repository's selection is read the way production reads it
(``core.resolve.default_name``, for a solution whose default is a declared implementation, and ``esoi.runtime.snapshot`` against the committed snapshot). Zero model calls."""
import json

import common
from esoi import leaderboard, runtime

from quest_ai_runner.core import resolve


def row_of(impl, spec):
    return {"impl": impl, "component": "evolve_by_metric", "passed": str(spec["passed"]), "cases": str(spec["cases"]),
            "cases_sha": spec.get("cases_sha", ""), "sample": spec.get("sample", ""),
            "model_calls": str(spec.get("model_calls", 0)), "tokens_in": str(spec.get("tokens", 0)), "tokens_out": "0",
            "cost_usd": "0.0", "balanced": ""}


def picked(solution_id):
    """What the running system would use by default, or ``None`` when the records name something it cannot resolve."""
    try:
        return resolve.default_name(solution_id)
    except Exception:  # noqa: BLE001  an unresolvable default is exactly what this case reports
        return None


def build(meter):  # noqa: ARG001
    def evolve(kind, args):
        if kind == "challenger":
            rows = {impl: row_of(impl, spec) for impl, spec in args["rows"].items()}
            if args["selected"] not in rows:
                return {"switch_to": None, "status": "unmeasured"}
            winners = leaderboard.challengers(rows, args["selected"], common.CONFIG.evaluation)
            if not winners:
                return {"switch_to": None, "status": "keep"}
            return {"switch_to": leaderboard.rank({w["impl"]: w for w in winners})[0]["impl"], "status": "switch"}
        if kind == "drift":
            return {"disagreeing": sorted(s for s, name in args["records"].items() if args["snapshot"].get(s) != name)}
        if kind == "repo_drift":
            shipped = json.loads((common.REPO / "quest_ai_runner" / "esoi_selection.json").read_text(encoding="utf-8"))
            fresh = runtime.snapshot(common.CONFIG)
            bad = {s for s in set(shipped) | set(fresh) if shipped.get(s) != fresh.get(s)}
            bad |= {s for s, entry in shipped.items() if entry.get("default") in entry.get("modules", {}) and picked(s) != entry["default"]}
            return {"disagreeing": sorted(bad), "checked": len(shipped)}
        raise ValueError(kind)
    return evolve
